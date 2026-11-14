"""The Starlette app behind ``wristpull serve``: login, pages, static files.

One process, one worker: the login rate limit (and in D3 the job registry) live in this process.
Every page handler is a plain function; Starlette runs it in a thread, and it opens its own
SQLite connection (WAL mode, so the CLI on the laptop can write at the same time).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from jinja2 import pass_context
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

import os

from .. import secrets as store
from ..agent import queue
from ..config import Config, default_config_path, validate_config, write_config
from ..db import connect
from . import jobview, markdown, pages
from .auth import RateLimiter, SessionCodec, hash_password, new_session, verify_password
from .i18n import pick_language, translations
from .jobs import JobRegistry

HERE = Path(__file__).parent
COOKIE = "wristpull_session"
INITIAL_PASSWORD = "wristpull"  # accepted only while no password hash exists anywhere; the login then forces a change
PASSWORD_PATHS = ("/settings/password", "/logout")  # reachable while the password must still be changed
CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"


@dataclass
class WebDeps:
    """Side effects the tests replace: the clock, the login limiter, the coach backend, the job runner."""

    today: Callable[[], date] | None = None  # None: today in the configured time zone
    now: Callable[[], datetime] | None = None  # None: the clock (UTC); tests fix it to judge the agent's heartbeat
    env: dict[str, str] | None = None  # None: os.environ (the settings page shows what .env decided)
    limiter: RateLimiter = field(default_factory=RateLimiter)
    coach_backend: Callable[[Config, str], Any] | None = None  # None: make_backend from the config
    jobs: JobRegistry | None = None  # None: a threaded registry on the configured database


def _number(value: float | None, digits: int, lang: str) -> str:
    if value is None:
        return "–"
    text = f"{value:,.{digits}f}"
    if lang == "de":
        text = text.replace(",", "\0").replace(".", ",").replace("\0", ".")
    return text


def format_value(value: float | None, kind: str, lang: str) -> str:
    """Tile values: ``int`` with thousands separators, ``hm`` hours as 7:12, ``float1`` one decimal."""
    if value is None:
        return "–"
    if kind == "hm":
        minutes = int(round(value * 60))
        return f"{minutes // 60}:{minutes % 60:02d}"
    return _number(value, 1 if kind == "float1" else 0, lang)


def format_delta(value: float | None, kind: str, lang: str) -> str:
    if value is None:
        return ""
    if kind == "hm":
        minutes = int(round(value * 60))
        sign = "+" if minutes > 0 else ("−" if minutes < 0 else "±")
        return f"{sign}{abs(minutes) // 60}:{abs(minutes) % 60:02d}"
    rounded = round(value)
    sign = "+" if rounded > 0 else ("−" if rounded < 0 else "±")
    return sign + _number(abs(rounded), 0, lang)


def format_metric(value: float | None, digits: int, lang: str, *, signed: bool = False) -> str:
    """Metric values with the registry's decimals; ``signed`` for deltas (+1.5 / −2 / ±0)."""
    if value is None:
        return "–" if not signed else ""
    rounded = round(value, digits)
    text = _number(abs(rounded) if signed else rounded, digits, lang)
    if not signed:
        return text
    sign = "+" if rounded > 0 else ("−" if rounded < 0 else "±")
    return sign + text


def format_duration(seconds: float | None) -> str:
    """``1:02:05`` or ``42:10``."""
    if seconds is None:
        return "–"
    total = int(round(seconds))
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_hm(seconds: float | None) -> str:
    """``7:44`` (hours and minutes, for sleep)."""
    if seconds is None:
        return "–"
    minutes = int(round(seconds / 60))
    return f"{minutes // 60}:{minutes % 60:02d}"


def format_km(metres: float | None, lang: str) -> str:
    return "–" if not metres else _number(metres / 1000, 2, lang) + " km"


def format_pace(seconds: float | None, metres: float | None) -> str:
    """min/km, only meaningful for runs and walks."""
    if not seconds or not metres or metres < 100:
        return "–"
    per_km = seconds / (metres / 1000)
    return f"{int(per_km // 60)}:{int(round(per_km % 60)):02d} /km"


def local_time(iso_utc: str | None, tz: ZoneInfo) -> str:
    if not iso_utc:
        return "–"
    moment = datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(tz)
    return moment.strftime("%d.%m. %H:%M")


def create_app(config: Config, *, password_hash: str | None = None, secret: str, deps: WebDeps | None = None) -> Starlette:
    """``password_hash`` is the environment's hash (may be None); a hash saved on the settings page wins, and
    without either the start password is accepted and must be changed at the first login."""
    deps = deps or WebDeps()
    codec = SessionCodec(secret)
    env = deps.env if deps.env is not None else os.environ
    jobs = deps.jobs or JobRegistry(config.db_path)
    tz = ZoneInfo(config.timezone)
    templates = Jinja2Templates(directory=str(HERE / "templates"))

    @pass_context
    def num(ctx: Any, value: float | None, digits: int = 0) -> str:
        return _number(value, digits, ctx.get("lang", "en"))

    @pass_context
    def km(ctx: Any, metres: float | None) -> str:
        return format_km(metres, ctx.get("lang", "en"))

    templates.env.filters["num"] = num
    templates.env.filters["markdown"] = markdown.render
    templates.env.filters["km"] = km
    templates.env.filters["dur"] = format_duration
    templates.env.filters["hm"] = format_hm
    templates.env.globals["pace"] = format_pace

    def today() -> date:
        return deps.today() if deps.today else datetime.now(tz).date()

    def now() -> datetime:
        return deps.now() if deps.now else datetime.now(UTC)

    def gettext_of(request: Request):
        gt = translations(pick_language(config.ui_language, request.headers.get("accept-language")))
        return gt.gettext, gt.ngettext

    def session_of(request: Request) -> dict[str, Any] | None:
        return codec.loads(request.cookies.get(COOKIE))

    def render(request: Request, name: str, session: dict[str, Any], context: dict[str, Any], *, status_code: int = 200, set_session: bool = False) -> Response:
        lang = pick_language(config.ui_language, request.headers.get("accept-language"))
        gt = translations(lang)
        base = {"_": gt.gettext, "ngettext": gt.ngettext, "lang": lang, "csrf": session["csrf"], "logged_in": bool(session.get("user")), "path": request.url.path}
        response = templates.TemplateResponse(request, name, {**base, **context}, status_code=status_code)
        if set_session:
            set_cookie(response, session)
        return response

    def set_cookie(response: Response, session: dict[str, Any]) -> None:
        response.set_cookie(COOKIE, codec.dumps(session), max_age=codec.max_age_s, httponly=True, secure=True, samesite="strict", path="/")

    def csrf_ok(request: Request, session: dict[str, Any] | None, form: Any) -> bool:
        sent = request.headers.get("x-csrf-token") or (form.get("csrf") if form is not None else None)
        return bool(session and sent and isinstance(sent, str) and sent == session.get("csrf"))

    def current_hash() -> str | None:
        """The password hash in force: the store (settings page) first, then the environment."""
        return store.load(config).get(store.PASSWORD_HASH) or password_hash or None

    def must_change(session: dict[str, Any]) -> bool:
        return bool(session.get("must_change")) and current_hash() is None

    def require_user(request: Request) -> dict[str, Any] | Response:
        session = session_of(request)
        if not session or not session.get("user"):
            return RedirectResponse("/login", status_code=303)
        if must_change(session) and request.url.path not in PASSWORD_PATHS:
            return RedirectResponse("/settings/password?first=1", status_code=303)
        return session

    def client_key(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    # ---- routes ----------------------------------------------------------------------

    def login_page(request: Request) -> Response:
        session = session_of(request)
        if session and session.get("user"):
            return RedirectResponse("/", status_code=303)
        session = session or new_session()
        return render(request, "login.html", session, {"error": None, "first_run": current_hash() is None}, set_session=True)

    async def login_submit(request: Request) -> Response:
        form = await request.form()
        session = session_of(request)
        if not csrf_ok(request, session, form):
            return PlainTextResponse("CSRF token missing or wrong", status_code=403)
        assert session is not None
        client = client_key(request)
        if deps.limiter.blocked(client):
            return render(request, "login.html", session, {"error": "blocked"}, status_code=429)
        encoded = current_hash()
        given = str(form.get("password") or "")
        first_login = encoded is None and given == INITIAL_PASSWORD
        if not first_login and not (encoded and verify_password(given, encoded)):
            deps.limiter.failure(client)
            return render(request, "login.html", session, {"error": "wrong", "first_run": encoded is None}, status_code=401)
        deps.limiter.success(client)
        response = RedirectResponse("/settings/password?first=1" if first_login else "/", status_code=303)
        fresh = new_session(user=True)  # a new CSRF token after login
        if first_login:
            fresh["must_change"] = True
        set_cookie(response, fresh)
        return response

    async def logout(request: Request) -> Response:
        form = await request.form()
        session = session_of(request)
        if not csrf_ok(request, session, form):
            return PlainTextResponse("CSRF token missing or wrong", status_code=403)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return response

    def today_page(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        lang = pick_language(config.ui_language, request.headers.get("accept-language"))
        with closing(connect(config.db_path)) as conn:
            data = pages.today_data(conn, today(), tz)
        for tile in data["tiles"]:
            tile["text"] = format_value(tile["value"], tile["kind"], lang)
            tile["delta_text"] = format_delta(tile["delta_28d"], tile["kind"], lang)
            tile["mean_text"] = format_value(tile["mean_28d"], tile["kind"], lang)
        charts_json = chart_json([t["spark"] for t in data["tiles"]] + ([data["curve"]] if data["curve"] else []), lang)
        imports = {source: local_time(stamp, tz) for source, stamp in data["imports"].items()}
        coach_time = local_time(data["coach"]["created_at_utc"], tz) if data["coach"] else None
        with closing(connect(config.db_path)) as conn:
            devices = pages.devices_data(conn, config, now=now())
            devices["agent"]["last_seen"] = local_time(devices["agent"]["last_seen"], tz) if devices["agent"]["last_seen"] else None
            devices["job"] = agent_view(conn, devices["job"], request) if devices["job"] else None
        return render(request, "today.html", session, {"page": "today", "d": data, "imports": imports, "coach_time": coach_time, "today": today(), "source_labels": pages.SOURCE_LABELS, "devices": devices, "charts_json": charts_json})

    def trends_page(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        try:
            weeks = int(request.query_params.get("weeks", "4"))
        except ValueError:
            weeks = 4
        lang = pick_language(config.ui_language, request.headers.get("accept-language"))
        with closing(connect(config.db_path)) as conn:
            data = pages.trends_data(conn, today(), weeks)
        for tile in data["tiles"]:
            tile["text"] = format_metric(tile["latest"], tile["digits"], lang)
            tile["delta_text"] = format_metric(tile["delta"], tile["digits"], lang, signed=True)
            tile["mean_text"] = format_metric(tile["mean_28d"], tile["digits"], lang)
        charts_json = chart_json([t["chart"] for t in data["tiles"]] + data["charts"], lang)  # "<" escaped: no "</script>" inside the data block
        return render(request, "trends.html", session, {"page": "trends", "d": data, "charts_json": charts_json, "choices": pages.TREND_WEEKS})

    def metric_page(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        lang = pick_language(config.ui_language, request.headers.get("accept-language"))
        key = request.path_params["key"]
        with closing(connect(config.db_path)) as conn:
            data = pages.metric_page(conn, key, today(), weeks_of(request), day=request.query_params.get("day"), compare=request.query_params.get("compare"), tz=tz)
        if data is None:
            return render(request, "missing.html", session, {"page": "trends"}, status_code=404)
        digits = data["m"].digits
        st = data["stats"]
        data["texts"] = {k: format_metric(st[k], digits, lang) for k in ("latest", "mean_7d", "mean_28d", "min", "max")}
        data["texts"]["z"] = format_metric(st["z"], 1, lang, signed=True) if st["z"] is not None else "–"
        for row in data["table"]:
            row["texts"] = {k: format_metric(row[k], digits, lang) for k in ("garmin", "strap", "mean")}
        charts = [data["chart"]] + ([data["compare_chart"]] if data["compare_chart"] else []) + [data["weekday"]]
        curve_json = chart_json([data["curve"]], lang) if data["curve"] else None
        return render(request, "metric.html", session, {"page": "trends", "d": data, "charts_json": chart_json(charts, lang), "curve_json": curve_json, "choices": pages.TREND_WEEKS})

    def metric_day(request: Request) -> Response:
        """The day's curve of a metric (htmx partial behind the ← → links and the chart clicks)."""
        session = require_user(request)
        if isinstance(session, Response):
            return session
        lang = pick_language(config.ui_language, request.headers.get("accept-language"))
        key = request.path_params["key"]
        spec = pages.series.spec(key)
        try:
            day = date.fromisoformat(request.query_params.get("day", ""))
        except ValueError:
            day = None
        if spec is None or not spec.samples or day is None:
            return PlainTextResponse("unknown metric or day", status_code=404)
        start, _weeks, _by_week = pages._window(today(), weeks_of(request))
        with closing(connect(config.db_path)) as conn:
            sample_days = pages.series.days_with_samples(conn, key, start, today())
            curve = pages.intraday_chart(conn, key, day, tz, sample_days)
        return render(request, "_intraday.html", session, {"page": "trends", "d": {"curve": curve, "m": spec, "weeks": weeks_of(request)}, "curve_json": chart_json([curve], lang)})

    def weeks_of(request: Request) -> int:
        try:
            return int(request.query_params.get("weeks", "4"))
        except ValueError:
            return 4

    def chart_json(charts: list[dict[str, Any]], lang: str) -> str:
        gt = translations(lang)
        for chart in charts:
            chart["title"] = gt.gettext(chart["title"])
            for series in chart["series"]:
                series["name"] = gt.gettext(series["name"])
        return json.dumps(charts, separators=(",", ":")).replace("<", "\\u003c")

    def activities_page(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        sport = request.query_params.get("sport") or None
        with closing(connect(config.db_path)) as conn:
            data = pages.activities_list(conn, today(), weeks_of(request), sport=sport, tz=tz)
        return render(request, "activities.html", session, {"page": "activities", "d": data, "choices": pages.TREND_WEEKS})

    def activity_page(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        lang = pick_language(config.ui_language, request.headers.get("accept-language"))
        with closing(connect(config.db_path)) as conn:
            data = pages.activity_page(conn, int(request.path_params["activity_id"]), tz)
        if data is None:
            return render(request, "missing.html", session, {"page": "activities"}, status_code=404)
        return render(request, "activity.html", session, {"page": "activities", "d": data, "charts_json": chart_json(data["charts"], lang)})

    def sleep_page(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        with closing(connect(config.db_path)) as conn:
            data = pages.sleep_list(conn, today(), weeks_of(request))
        return render(request, "sleep.html", session, {"page": "sleep", "d": data, "choices": pages.TREND_WEEKS})

    def night_page(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        with closing(connect(config.db_path)) as conn:
            data = pages.night_page(conn, int(request.path_params["sleep_id"]), tz)
        if data is None:
            return render(request, "missing.html", session, {"page": "sleep"}, status_code=404)
        return render(request, "night.html", session, {"page": "sleep", "d": data})

    async def posted(request: Request) -> tuple[dict[str, Any], Any] | Response:
        """Session and form of a logged-in POST with a valid CSRF token, else the response to send."""
        session = session_of(request)
        if not session or not session.get("user"):
            return RedirectResponse("/login", status_code=303)
        if must_change(session) and request.url.path not in PASSWORD_PATHS:
            return RedirectResponse("/settings/password?first=1", status_code=303)
        form = await request.form()
        if not csrf_ok(request, session, form):
            return PlainTextResponse("CSRF token missing or wrong", status_code=403)
        return session, form

    # ---- settings: the config file, the secrets store, the password ------------------------

    def settings_page(request: Request, session: dict[str, Any], *, problems: list[str] | None = None, saved: str | None = None, status_code: int = 200) -> Response:
        data = pages.settings_data(config, dict(env))
        return render(request, "settings.html", session, {"page": "settings", "d": data, "problems": problems or [], "saved": saved or request.query_params.get("saved")}, status_code=status_code)

    def settings_get(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        return settings_page(request, session)

    async def settings_config(request: Request) -> Response:
        got = await posted(request)
        if isinstance(got, Response):
            return got
        session, form = got
        cfg, problems = pages.config_from_form(config, form)
        problems += validate_config(cfg)
        if problems:
            return settings_page(request, session, problems=problems, status_code=400)
        target = config.path or default_config_path(env)
        write_config(cfg, target)
        cfg.path = target
        pages.apply_config(config, cfg)
        return RedirectResponse("/settings?saved=config", status_code=303)

    async def settings_secrets(request: Request) -> Response:
        got = await posted(request)
        if isinstance(got, Response):
            return got
        _session, form = got
        changes: dict[str, str | None] = {}
        for key, _label, _hint in pages.SECRET_FIELDS:
            value = str(form.get(key) or "").strip()
            if form.get(f"clear_{key}") is not None:
                changes[key] = None
            elif value:
                changes[key] = value
        if "ollama_url" in form:
            url = str(form.get("ollama_url") or "").strip()
            changes[store.OLLAMA_URL] = url or None
            config.coach.ollama_url = url or config.coach.ollama_url
        store.update(config, changes)
        store.apply(config)
        return RedirectResponse("/settings?saved=secrets", status_code=303)

    def password_get(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        return render(request, "password.html", session, {"page": "settings", "first": bool(request.query_params.get("first")) or must_change(session), "error": None, "minimum": pages.PASSWORD_MIN})

    async def password_post(request: Request) -> Response:
        got = await posted(request)
        if isinstance(got, Response):
            return got
        session, form = got
        current, new, repeat = (str(form.get(k) or "") for k in ("current", "new", "repeat"))
        encoded = current_hash()
        first = encoded is None
        error = None
        if first and current != INITIAL_PASSWORD or (not first and not verify_password(current, encoded)):
            error = "current"
        elif len(new) < pages.PASSWORD_MIN or new == INITIAL_PASSWORD:
            error = "weak"
        elif new != repeat:
            error = "repeat"
        if error:
            return render(request, "password.html", session, {"page": "settings", "first": first, "error": error, "minimum": pages.PASSWORD_MIN}, status_code=400)
        store.update(config, {store.PASSWORD_HASH: hash_password(new)})
        response = RedirectResponse("/?saved=password" if first else "/settings?saved=password", status_code=303)
        set_cookie(response, new_session(user=True))  # drops must_change, fresh CSRF token
        return response

    def is_htmx(request: Request) -> bool:
        return request.headers.get("hx-request") == "true"

    def job_partial(request: Request, session: dict[str, Any], *, refresh: bool = False, busy: bool = False) -> Response:
        """The coach run of this process (polled at /coach/job)."""
        current = jobs.current()
        view = jobview.from_web(current) if current else None
        response = render(request, "_job.html", session, {"job": view, "busy": busy, "poll_url": "/coach/job", "show_log": False, "offline": False})
        if refresh:
            response.headers["HX-Refresh"] = "true"
        return response

    def agent_view(conn: Any, job: queue.AgentJob, request: Request, *, with_log: bool = True) -> jobview.JobView:
        gettext, ngettext = gettext_of(request)
        return jobview.from_agent(job, log=queue.log_lines(conn, job.id) if with_log else None, gettext=gettext, ngettext=ngettext, now=now())

    def agent_partial(request: Request, session: dict[str, Any], *, refresh: bool = False, busy: bool = False, error: str | None = None) -> Response:
        """The device agent's current job (polled at /sync/job)."""
        with closing(connect(config.db_path)) as conn:
            job = queue.current(conn)
            view = agent_view(conn, job, request) if job else None
            online = queue.device_state(conn, now=now())["online"]
        context = {"job": view, "busy": busy, "poll_url": "/sync/job", "show_log": True, "offline": bool(view and view.active and not online), "error": error}
        response = render(request, "_job.html", session, context)
        if refresh:
            response.headers["HX-Refresh"] = "true"
        return response

    def sync_page(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        with closing(connect(config.db_path)) as conn:
            data = pages.sync_data(conn, config, now=now(), tz=tz)
            data["agent"]["last_seen"] = local_time(data["agent"]["last_seen"], tz) if data["agent"]["last_seen"] else None
            view = agent_view(conn, data["job"], request) if data["job"] else None
        return render(request, "sync.html", session, {"page": "sync", "d": data, "job": view, "busy": False, "error": request.query_params.get("error")})

    async def sync_run(request: Request) -> Response:
        """Enqueue a device job for the agent; the browser polls /sync/job."""
        got = await posted(request)
        if isinstance(got, Response):
            return got
        session, form = got
        kind = str(form.get("kind") or "")
        params = {key: form.getlist(key) if key in ("names", "keys") else str(form.get(key)) for key in form.keys() if key not in ("csrf", "kind")}
        try:
            with closing(connect(config.db_path)) as conn:
                job = queue.enqueue(conn, kind, params, now=now())
        except ValueError as exc:
            return PlainTextResponse(str(exc), status_code=400)
        if is_htmx(request):
            return agent_partial(request, session, busy=job is None)
        return RedirectResponse("/sync", status_code=303)

    def sync_job(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        with closing(connect(config.db_path)) as conn:
            job = queue.current(conn)
        # The page polls while a job is queued or running; the first answer after it finished reloads the page once.
        return agent_partial(request, session, refresh=bool(request.query_params.get("poll") and job and not job.active))

    async def sync_stop(request: Request) -> Response:
        got = await posted(request)
        if isinstance(got, Response):
            return got
        session, form = got
        try:
            job_id = int(str(form.get("job_id")))
        except ValueError:
            return PlainTextResponse("bad job id", status_code=400)
        with closing(connect(config.db_path)) as conn:
            queue.request_stop(conn, job_id)
        if is_htmx(request):
            return agent_partial(request, session)
        return RedirectResponse("/sync", status_code=303)

    def coach_page(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        with closing(connect(config.db_path)) as conn:
            data = pages.coach_data(conn, tz)
        current = jobs.current()
        return render(request, "coach.html", session, {"page": "coach", "d": data, "job": jobview.from_web(current) if current else None, "busy": False,
                                                       "poll_url": "/coach/job", "show_log": False, "offline": False, "today": today().isoformat(),
                                                       "saved": request.query_params.get("saved")})

    async def coach_run(request: Request) -> Response:
        got = await posted(request)
        if isinstance(got, Response):
            return got
        session, form = got
        kind = str(form.get("kind") or "")
        question = str(form.get("question") or "").strip() or None
        if kind not in pages.COACH_KINDS or (kind == "ask" and not question):
            return PlainTextResponse("unknown run kind or empty question", status_code=400)
        day = today()

        def run(conn: Any, log: Callable[[str], None]) -> dict[str, Any]:
            from ..coach.runner import run_coach

            store.apply(config)  # API keys saved on the settings page override the environment
            backend = deps.coach_backend(config, kind) if deps.coach_backend else None
            result = run_coach(conn, config, kind, today=day, question=question, backend=backend, log=log)
            return {"run_id": result.run_id, "warnings": result.warnings}

        started = jobs.start(f"coach-{kind}", kind, run)
        if is_htmx(request):
            return job_partial(request, session, busy=started is None)
        return RedirectResponse("/coach", status_code=303)

    def coach_job(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        job = jobs.current()
        # The page polls while a job runs; the first answer after it finished reloads the page once.
        return job_partial(request, session, refresh=bool(request.query_params.get("poll") and job and not job.running))

    async def coach_rate(request: Request) -> Response:
        got = await posted(request)
        if isinstance(got, Response):
            return got
        _session, form = got
        try:
            run_id, rating = int(str(form.get("run_id"))), int(str(form.get("rating")))
        except ValueError:
            return PlainTextResponse("bad rating", status_code=400)
        if rating not in (-1, 0, 1):
            return PlainTextResponse("bad rating", status_code=400)
        from ..coach.store import add_feedback, get_run

        with closing(connect(config.db_path)) as conn:
            if get_run(conn, run_id) is None:
                return PlainTextResponse("unknown run", status_code=404)
            add_feedback(conn, run_id, rating, str(form.get("note") or "").strip() or None)
        target = "/" if form.get("next") == "/" else f"/coach#run-{run_id}"
        return RedirectResponse(target, status_code=303)

    async def journal_add(request: Request) -> Response:
        got = await posted(request)
        if isinstance(got, Response):
            return got
        _session, form = got
        note = str(form.get("note") or "").strip()
        if note:
            from ..coach.store import add_note

            with closing(connect(config.db_path)) as conn:
                add_note(conn, today().isoformat(), note[:2000])
        return RedirectResponse("/coach?saved=note#journal", status_code=303)

    def workouts_page(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        with closing(connect(config.db_path)) as conn:
            data = pages.workouts_data(config, conn, today())
        q = request.query_params
        return render(request, "workouts.html", session, {"page": "workouts", "d": data, "drafts": q.get("drafts"), "skipped": q.get("skipped"), "message": q.get("message")})

    async def workout_new(request: Request) -> Response:
        got = await posted(request)
        if isinstance(got, Response):
            return got
        _session, form = got
        try:
            day = date.fromisoformat(str(form.get("date") or ""))
        except ValueError:
            day = today()
        path = pages.new_workout(config, str(form.get("kind") or "time"), day)
        return RedirectResponse(f"/workouts/edit?file={quote(path.name)}", status_code=303)

    async def workout_drafts(request: Request) -> Response:
        got = await posted(request)
        if isinstance(got, Response):
            return got
        from ..workouts.drafts import drafts_from_plan

        with closing(connect(config.db_path)) as conn:
            result = drafts_from_plan(conn, config)
        if result.message:
            return RedirectResponse("/workouts?message=noplan", status_code=303)
        return RedirectResponse(f"/workouts?drafts={len(result.created)}&skipped={len(result.skipped)}", status_code=303)

    def workout_edit(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        from ..workouts.store import check_text, workout_file

        name = request.query_params.get("file", "")
        try:
            path = workout_file(config, name)
        except ValueError:
            return render(request, "missing.html", session, {"page": "workouts"}, status_code=404)
        if not path.is_file():
            return render(request, "missing.html", session, {"page": "workouts"}, status_code=404)
        text = path.read_text("utf-8")
        check = pages.check_lines(check_text(config, name, text))
        return render(request, "workout_edit.html", session, {"page": "workouts", "file": name, "text": text, "check": check,
                                                             "saved": request.query_params.get("saved")})

    async def workout_check(request: Request) -> Response:
        got = await posted(request)
        if isinstance(got, Response):
            return got
        session, form = got
        from ..workouts.store import check_text

        try:
            workout = check_text(config, str(form.get("file") or "") or None, str(form.get("text") or ""))
        except ValueError:
            return PlainTextResponse("bad file name", status_code=400)
        return render(request, "_check.html", session, {"check": pages.check_lines(workout)})

    async def workout_save(request: Request) -> Response:
        got = await posted(request)
        if isinstance(got, Response):
            return got
        _session, form = got
        from ..workouts.store import write_text

        name = str(form.get("file") or "")
        try:
            write_text(config, name, str(form.get("text") or ""))
        except FileNotFoundError:
            return PlainTextResponse("unknown workout file", status_code=404)
        except ValueError as exc:
            return PlainTextResponse(str(exc), status_code=400)
        return RedirectResponse(f"/workouts/edit?file={quote(name)}&saved=1", status_code=303)

    def healthz(request: Request) -> Response:
        return JSONResponse({"ok": True})

    async def headers(request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    app = Starlette(
        routes=[
            Route("/", today_page),
            Route("/trends", trends_page),
            Route("/metrics/{key}", metric_page),
            Route("/metrics/{key}/day", metric_day),
            Route("/activities", activities_page),
            Route("/activities/{activity_id:int}", activity_page),
            Route("/sleep", sleep_page),
            Route("/sleep/{sleep_id:int}", night_page),
            Route("/settings", settings_get),
            Route("/settings/config", settings_config, methods=["POST"]),
            Route("/settings/secrets", settings_secrets, methods=["POST"]),
            Route("/settings/password", password_get),
            Route("/settings/password", password_post, methods=["POST"]),
            Route("/sync", sync_page),
            Route("/sync/run", sync_run, methods=["POST"]),
            Route("/sync/job", sync_job),
            Route("/sync/stop", sync_stop, methods=["POST"]),
            Route("/coach", coach_page),
            Route("/coach/run", coach_run, methods=["POST"]),
            Route("/coach/job", coach_job),
            Route("/coach/rate", coach_rate, methods=["POST"]),
            Route("/journal", journal_add, methods=["POST"]),
            Route("/workouts", workouts_page),
            Route("/workouts/new", workout_new, methods=["POST"]),
            Route("/workouts/drafts", workout_drafts, methods=["POST"]),
            Route("/workouts/edit", workout_edit),
            Route("/workouts/check", workout_check, methods=["POST"]),
            Route("/workouts/save", workout_save, methods=["POST"]),
            Route("/login", login_page, methods=["GET"]),
            Route("/login", login_submit, methods=["POST"]),
            Route("/logout", logout, methods=["POST"]),
            Route("/healthz", healthz),
            Mount("/static", app=StaticFiles(directory=str(HERE / "static")), name="static"),
        ],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=headers)],
    )
    return app
