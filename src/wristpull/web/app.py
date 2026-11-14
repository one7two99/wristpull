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
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

from jinja2 import pass_context
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

import os

from .. import secrets as store
from ..agent import queue
from ..config import UI_LANGUAGES, Config, default_config_path, validate_config, write_config
from ..db import connect
from . import jobview, markdown, pages
from .files import BadPath
from .auth import RateLimiter, SessionCodec, hash_password, new_session, verify_password
from .i18n import LANG_COOKIE, pick_language, translations
from .jobs import JobRegistry

HERE = Path(__file__).parent
COOKIE = "wristpull_session"
INITIAL_PASSWORD = "wristpull"  # accepted only while no password hash exists anywhere; the login then forces a change
PASSWORD_PATHS = ("/settings/password", "/logout")  # reachable while the password must still be changed
BACKUP_PREFIX = "/backup"  # also reachable then: a fresh instance may be restored from an archive instead of set up
UPLOAD_CHUNK = 1 << 20
CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"


def allowed_while_must_change(path: str) -> bool:
    """The pages a start-password session may use: change the password, log out, or restore a backup."""
    return path in PASSWORD_PATHS or path == BACKUP_PREFIX or path.startswith(BACKUP_PREFIX + "/")


def restart_process() -> None:
    """Ask the server to stop (Docker starts it again: ``restart: unless-stopped``); the response goes out first."""
    import signal
    import threading

    threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGTERM)).start()


@dataclass
class WebDeps:
    """Side effects the tests replace: the clock, the login limiter, the coach backend, the job runner, the restart."""

    today: Callable[[], date] | None = None  # None: today in the configured time zone
    now: Callable[[], datetime] | None = None  # None: the clock (UTC); tests fix it to judge the agent's heartbeat
    env: dict[str, str] | None = None  # None: os.environ (the settings page shows what .env decided)
    limiter: RateLimiter = field(default_factory=RateLimiter)
    coach_backend: Callable[[Config, str], Any] | None = None  # None: make_backend from the config
    jobs: JobRegistry | None = None  # None: a threaded registry on the configured database
    restart: Callable[[], None] | None = None  # None: restart_process (only offered inside the container)


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
    # cache buster for the app's own CSS/JS: browsers keep /static/ files, a new image must not serve stale charts
    templates.env.globals["asset_v"] = str(int(max(p.stat().st_mtime for p in (HERE / "static").glob("*.*"))))

    @pass_context
    def num(ctx: Any, value: float | None, digits: int = 0) -> str:
        return _number(value, digits, ctx.get("lang", "en"))

    @pass_context
    def km(ctx: Any, metres: float | None) -> str:
        return format_km(metres, ctx.get("lang", "en"))

    templates.env.filters["num"] = num
    templates.env.filters["markdown"] = markdown.render

    @pass_context
    def nbytes(ctx: Any, size: int | None) -> str:
        from ..tui.format import fmt_bytes

        return fmt_bytes(size, ctx.get("lang", "en"))

    templates.env.filters["bytes"] = nbytes
    templates.env.filters["km"] = km
    templates.env.filters["dur"] = format_duration
    templates.env.filters["hm"] = format_hm
    templates.env.globals["pace"] = format_pace

    def today() -> date:
        return deps.today() if deps.today else datetime.now(tz).date()

    def now() -> datetime:
        return deps.now() if deps.now else datetime.now(UTC)

    def lang_of(request: Request) -> str:
        return pick_language(config.ui_language, request.headers.get("accept-language"), request.cookies.get(LANG_COOKIE))

    def gettext_of(request: Request):
        gt = translations(lang_of(request))
        return gt.gettext, gt.ngettext

    def session_of(request: Request) -> dict[str, Any] | None:
        return codec.loads(request.cookies.get(COOKIE))

    def render(request: Request, name: str, session: dict[str, Any], context: dict[str, Any], *, status_code: int = 200, set_session: bool = False) -> Response:
        lang = lang_of(request)
        gt = translations(lang)
        base = {"_": gt.gettext, "ngettext": gt.ngettext, "lang": lang, "languages": UI_LANGUAGES, "csrf": session["csrf"], "logged_in": bool(session.get("user")), "path": request.url.path}
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

    def restored_logout(request: Request) -> Response:
        """A start-password session after a restore installed a real hash: it ends, the archive's password counts.
        htmx polls get the redirect as a header (a 303 would be followed inside the XHR and swapped into the page)."""
        if request.headers.get("hx-request"):
            response: Response = PlainTextResponse("", headers={"HX-Redirect": "/login?restored=1"})
        else:
            response = RedirectResponse("/login?restored=1", status_code=303)
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return response

    def require_user(request: Request) -> dict[str, Any] | Response:
        session = session_of(request)
        if not session or not session.get("user"):
            return RedirectResponse("/login", status_code=303)
        if session.get("must_change") and current_hash() is not None:
            return restored_logout(request)
        if must_change(session) and not allowed_while_must_change(request.url.path):
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
        return render(request, "login.html", session, {"error": None, "first_run": current_hash() is None, "restored": bool(request.query_params.get("restored"))}, set_session=True)

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
        lang = lang_of(request)
        with closing(connect(config.db_path)) as conn:
            data = pages.today_data(conn, today(), tz)
            data["readiness"] = pages.readiness_tile(conn, config, today())
            from ..insights.store import tags_for
            from ..insights.tags import TAGS

            data["today_tags"] = tags_for(conn, today().isoformat())
            data["all_tags"] = TAGS
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
        lang = lang_of(request)
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
        lang = lang_of(request)
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
        lang = lang_of(request)
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
            if chart.get("stage_labels"):
                chart["labels"] = [gt.gettext(label) for label in chart["labels"]]
            if chart.get("stages"):
                chart["stage_names"] = [gt.gettext(stage) for stage in chart["stages"]]
        return json.dumps(charts, separators=(",", ":")).replace("<", "\\u003c")

    def activities_page(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        sport = request.query_params.get("sport") or None
        q = (request.query_params.get("q") or "")[:80]
        lang = lang_of(request)
        with closing(connect(config.db_path)) as conn:
            data = pages.activities_list(conn, today(), weeks_of(request), sport=sport, q=q, tz=tz)
        charts_json = chart_json([data["weekly"]], lang) if data["weekly"] else None
        return render(request, "activities.html", session, {"page": "activities", "d": data, "choices": pages.TREND_WEEKS, "charts_json": charts_json})

    def activity_page(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        lang = lang_of(request)
        with closing(connect(config.db_path)) as conn:
            data = pages.activity_page(conn, int(request.path_params["activity_id"]), tz)
        if data is None:
            return render(request, "missing.html", session, {"page": "activities"}, status_code=404)
        charts = data["charts"] + [c for c in (data["donut"], data["lap_chart"], data["track"]) if c]
        return render(request, "activity.html", session, {"page": "activities", "d": data, "charts_json": chart_json(charts, lang)})

    def sleep_page(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        with closing(connect(config.db_path)) as conn:
            data = pages.sleep_list(conn, today(), weeks_of(request), source=request.query_params.get("source") or "all", tz=tz)
        return render(request, "sleep.html", session, {"page": "sleep", "d": data, "choices": pages.TREND_WEEKS, "ticks": pages.NIGHT_AXIS_TICKS})

    def night_page(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        lang = lang_of(request)
        with closing(connect(config.db_path)) as conn:
            data = pages.night_page(conn, int(request.path_params["sleep_id"]), tz)
        if data is None:
            return render(request, "missing.html", session, {"page": "sleep"}, status_code=404)
        return render(request, "night.html", session, {"page": "sleep", "d": data, "charts_json": chart_json(data["charts"], lang)})

    async def posted(request: Request) -> tuple[dict[str, Any], Any] | Response:
        """Session and form of a logged-in POST with a valid CSRF token, else the response to send."""
        session = session_of(request)
        if not session or not session.get("user"):
            return RedirectResponse("/login", status_code=303)
        if session.get("must_change") and current_hash() is not None:
            return restored_logout(request)
        if must_change(session) and not allowed_while_must_change(request.url.path):
            return RedirectResponse("/settings/password?first=1", status_code=303)
        form = await request.form()
        if not csrf_ok(request, session, form):
            return PlainTextResponse("CSRF token missing or wrong", status_code=403)
        return session, form

    # ---- settings: the config file, the secrets store, the password ------------------------

    CHECK_KEYS = ("coach-check",)

    def settings_page(request: Request, session: dict[str, Any], *, problems: list[str] | None = None, saved: str | None = None, status_code: int = 200) -> Response:
        data = pages.settings_data(config, dict(env))
        current = jobs.last_of(CHECK_KEYS)
        context = {"page": "settings", "d": data, "problems": problems or [], "saved": saved or request.query_params.get("saved"),
                   "job": web_view(current, request) if current else None, "busy": False, "poll_url": "/settings/job", "show_log": False, "offline": False}
        return render(request, "settings.html", session, context, status_code=status_code)

    async def settings_check(request: Request) -> Response:
        """Ask the configured coach backend whether it is reachable (a background job: Ollama may need a moment)."""
        got = await posted(request)
        if isinstance(got, Response):
            return got
        session, _form = got

        def run(conn: Any, log: Callable[[str], None]) -> str:
            from ..coach.backends import CoachError, make_backend

            store.apply(config)
            try:
                backend = deps.coach_backend(config, "daily") if deps.coach_backend else make_backend(config)
                return str(backend.check())
            except CoachError as exc:
                raise RuntimeError(str(exc)) from exc

        started = jobs.start("coach-check", "Coach backend check", run, cli=f"wristpull coach daily --dry-run  # backend {config.coach.backend}/{config.coach.model}")
        if is_htmx(request):
            return job_partial(request, session, keys=CHECK_KEYS, poll_url="/settings/job", busy=started is None)
        return RedirectResponse("/settings#coach", status_code=303)

    def settings_job(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        job = jobs.last_of(CHECK_KEYS)
        return job_partial(request, session, keys=CHECK_KEYS, poll_url="/settings/job", refresh=bool(request.query_params.get("poll") and job and not job.running))

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

    COACH_KEYS = tuple(f"coach-{k}" for k in pages.COACH_KINDS)
    EXPORT_KEYS = ("export", "report-week", "report-month")

    def web_view(job: Any, request: Request) -> jobview.JobView:
        gettext, ngettext = gettext_of(request)
        return jobview.from_web(job, gettext=gettext, ngettext=ngettext)

    def job_partial(request: Request, session: dict[str, Any], *, keys: tuple[str, ...] = (), poll_url: str = "/coach/job", refresh: bool = False, busy: bool = False) -> Response:
        """The latest in-process job of the given kinds (coach runs at /coach/job, exports at /export/job)."""
        current = jobs.last_of(keys or COACH_KEYS)
        view = web_view(current, request) if current else None
        response = render(request, "_job.html", session, {"job": view, "busy": busy, "poll_url": poll_url, "show_log": False, "offline": False})
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
            data = pages.coach_data(conn, tz, config=config, lang=lang_of(request))
        current = jobs.last_of(COACH_KEYS)
        return render(request, "coach.html", session, {"page": "coach", "d": data, "job": web_view(current, request) if current else None, "busy": False,
                                                       "poll_url": "/coach/job", "show_log": False, "offline": False, "today": today().isoformat(),
                                                       "saved": request.query_params.get("saved")})

    async def coach_run(request: Request) -> Response:
        got = await posted(request)
        if isinstance(got, Response):
            return got
        session, form = got
        kind = str(form.get("kind") or "")
        question = str(form.get("question") or "").strip() or None
        dry_run = form.get("dry_run") is not None
        if kind not in pages.COACH_KINDS or (kind == "ask" and not question):
            return PlainTextResponse("unknown run kind or empty question", status_code=400)
        day = today()
        try:
            weeks = int(str(form.get("weeks") or "12"))
        except ValueError:
            weeks = 12
        weeks = weeks if weeks in pages.INSIGHT_WEEKS else 12
        back = "/insights" if kind == "insights" and form.get("from") == "insights" else "/coach"

        def run(conn: Any, log: Callable[[str], None]) -> dict[str, Any]:
            from ..coach.runner import run_coach

            store.apply(config)  # API keys saved on the settings page override the environment
            backend = deps.coach_backend(config, kind) if deps.coach_backend else None
            result = run_coach(conn, config, kind, today=day, question=question, backend=backend, dry_run=dry_run, weeks=weeks, log=log)
            return {"run_id": result.run_id, "warnings": result.warnings, "dry_run_path": str(result.dry_run_path) if result.dry_run_path else None}

        started = jobs.start(f"coach-{kind}", kind, run, cli=f"wristpull coach {kind}" + (f' "{question}"' if question else "") + (f" --weeks {weeks}" if kind == "insights" and weeks != 12 else "") + (" --dry-run" if dry_run else ""))
        if is_htmx(request):
            return job_partial(request, session, busy=started is None)
        return RedirectResponse(back, status_code=303)

    def coach_job(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        job = jobs.last_of(COACH_KEYS)
        # The page polls while a job runs; the first answer after it finished reloads the page once.
        return job_partial(request, session, refresh=bool(request.query_params.get("poll") and job and not job.running))

    # ---- insights and day tags (analysis wave A3) -----------------------------------------

    def insights_page(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        lang = lang_of(request)
        try:
            weeks = int(request.query_params.get("weeks", "12"))
        except ValueError:
            weeks = 12
        with closing(connect(config.db_path)) as conn:
            data = pages.insights_data(conn, config, today(), weeks, lang=lang)
        current = jobs.last_of(("coach-insights",))
        report_time = local_time(data["report"]["created_at_utc"], tz) if data["report"] else None
        return render(request, "insights.html", session, {"page": "insights", "d": data, "charts_json": chart_json(data["charts"], lang), "choices": pages.INSIGHT_WEEKS, "saved": request.query_params.get("saved"), "report_time": report_time,
                                                          "job": web_view(current, request) if current else None, "busy": False, "poll_url": "/coach/job", "show_log": False, "offline": False})

    def report_page(request: Request) -> Response:
        """The weekly or monthly report as a page: the CLI's Markdown plus the period's charts."""
        session = require_user(request)
        if isinstance(session, Response):
            return session
        lang = lang_of(request)
        kind = request.query_params.get("kind", "week")
        try:
            day = date.fromisoformat(request.query_params.get("date", "")) if request.query_params.get("date") else today()
        except ValueError:
            day = today()
        with closing(connect(config.db_path)) as conn:
            data = pages.report_page(conn, config, kind, day)
        if data is None:
            return render(request, "missing.html", session, {"page": "export"}, status_code=404)
        data["html"] = markdown.render(data["markdown"])
        return render(request, "report.html", session, {"page": "export", "d": data, "charts_json": chart_json(data["charts"], lang)})

    async def tags_post(request: Request) -> Response:
        """The checkbox form of one day: its tags become exactly the ticked ones."""
        got = await posted(request)
        if isinstance(got, Response):
            return got
        _session, form = got
        from ..insights.store import replace_tags

        try:
            day = date.fromisoformat(str(form.get("date") or ""))
        except ValueError:
            return PlainTextResponse("bad date", status_code=400)
        if abs((today() - day).days) > 366:
            return PlainTextResponse("date out of range", status_code=400)
        with closing(connect(config.db_path)) as conn:
            replace_tags(conn, day.isoformat(), [str(t) for t in form.getlist("tag")])
        target = str(form.get("next") or "/insights")
        if not target.startswith("/") or target.startswith("//"):
            target = "/insights"
        return RedirectResponse(target, status_code=303)

    # ---- days, files, export (parity wave P2) ----------------------------------------------

    def days_page(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        lang = lang_of(request)
        q = request.query_params
        start, end = pages.day_period(today(), config.ui_range_days, start=q.get("from"), end=q.get("to"), preset=q.get("preset"))
        with closing(connect(config.db_path)) as conn:
            data = pages.days_data(conn, start, end, day=q.get("day"), lang=lang)
        return render(request, "days.html", session, {"page": "days", "d": data})

    def day_detail(request: Request) -> Response:
        """One day's detail (htmx partial behind the table rows)."""
        session = require_user(request)
        if isinstance(session, Response):
            return session
        lang = lang_of(request)
        try:
            day = date.fromisoformat(request.query_params.get("day", ""))
        except ValueError:
            return PlainTextResponse("bad day", status_code=404)
        with closing(connect(config.db_path)) as conn:
            data = pages.days_data(conn, day, day, day=day.isoformat(), lang=lang)
        if data["detail"] is None:
            return PlainTextResponse("no data for that day", status_code=404)
        return render(request, "_day_detail.html", session, {"page": "days", "detail": data["detail"]})

    def files_page(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        q = request.query_params
        try:
            with closing(connect(config.db_path)) as conn:
                data = pages.files_data(conn, config, q.get("dir"), file=q.get("file") or None, env=dict(env))
        except BadPath:
            return render(request, "missing.html", session, {"page": "files"}, status_code=404)
        return render(request, "files.html", session, {"page": "files", "d": data})

    def files_download(request: Request) -> Response:
        """A file of the data directory (never adb/ or staging/), as an attachment."""
        session = require_user(request)
        if isinstance(session, Response):
            return session
        try:
            path = pages.filesafe.safe_path(config.data_dir, request.query_params.get("path"))
        except BadPath:
            return PlainTextResponse("not found", status_code=404)
        if not path.is_file():
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(str(path), filename=path.name, media_type="application/octet-stream")

    def export_page(request: Request, session: dict[str, Any] | None = None, *, error: str | None = None, status_code: int = 200) -> Response:
        session = session or require_user(request)
        if isinstance(session, Response):
            return session
        current = jobs.last_of(EXPORT_KEYS)
        view = web_view(current, request) if current else None
        result = pages.export_result(current.result, config, env=dict(env)) if current and current.status == "done" else None
        if result:
            result["preview_html"] = markdown.render(result["preview"])
        form = pages.export_form(today(), config.ui_range_days)
        return render(request, "export.html", session, {"page": "export", "form": form, "job": view, "result": result, "busy": False, "poll_url": "/export/job", "show_log": False, "offline": False, "error": error or request.query_params.get("error")}, status_code=status_code)

    async def export_run(request: Request) -> Response:
        """Start an export or a report as an in-process job (the same registry as the coach runs)."""
        got = await posted(request)
        if isinstance(got, Response):
            return got
        session, form = got
        kind = str(form.get("kind") or "export")
        if kind not in pages.EXPORT_KINDS:
            return PlainTextResponse("unknown kind", status_code=400)
        try:
            start = date.fromisoformat(str(form.get("from") or ""))
            end = date.fromisoformat(str(form.get("to") or ""))
        except ValueError:
            return export_page(request, session, error="dates", status_code=400)
        if end < start:
            return export_page(request, session, error="order", status_code=400)
        csv = form.get("csv") is not None
        charts = form.get("charts") is not None
        cli = pages.export_cli(kind, start, end, csv=csv, charts=charts)

        def run(conn: Any, log: Callable[[str], None]) -> Any:
            if kind == "export":
                from ..export import export_for_claude

                log(f"exporting {start.isoformat()} to {end.isoformat()} …")
                return export_for_claude(conn, config, start.isoformat(), end.isoformat(), write_csv=csv)
            from ..report import write_report

            log(f"rendering {kind} report for {end.isoformat()} …")
            return write_report(conn, config, kind, end, charts=charts)

        started = jobs.start("export" if kind == "export" else f"report-{kind}", pages.EXPORT_TITLES[kind], run, cli=cli)
        if is_htmx(request):
            return job_partial(request, session, keys=EXPORT_KEYS, poll_url="/export/job", busy=started is None)
        return RedirectResponse("/export", status_code=303)

    def export_job(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        job = jobs.last_of(EXPORT_KEYS)
        return job_partial(request, session, keys=EXPORT_KEYS, poll_url="/export/job", refresh=bool(request.query_params.get("poll") and job and not job.running))

    def coach_profile_get(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        data = pages.coach_profile_data(config, lang_of(request))
        return render(request, "coach_profile.html", session, {"page": "coach", "d": data, "saved": request.query_params.get("saved")})

    async def coach_profile_post(request: Request) -> Response:
        got = await posted(request)
        if isinstance(got, Response):
            return got
        _session, form = got
        text = str(form.get("text") or "")
        if len(text) > 100_000:
            return PlainTextResponse("profile too long", status_code=400)
        pages.save_coach_profile(config, text)
        return RedirectResponse("/coach/profile?saved=1", status_code=303)

    def coach_wizard_get(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        return render(request, "coach_wizard.html", session, {"page": "coach", "d": pages.wizard_data(config, lang_of(request))})

    async def coach_wizard_post(request: Request) -> Response:
        got = await posted(request)
        if isinstance(got, Response):
            return got
        _session, form = got
        from ..coach.profile import apply_wizard
        from ..coach.runner import resolve_language

        answers = pages.wizard_answers(form)
        apply_wizard(config, answers, resolve_language(config, lang_of(request)))
        return RedirectResponse("/coach/profile?saved=wizard", status_code=303)

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
        wanted = str(form.get("next") or "")
        target = "/" if wanted == "/" else ("/insights#report" if wanted == "/insights" else f"/coach#run-{run_id}")
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

    WORKOUT_JOB_KINDS = ("workout_push", "workout_push_upcoming", "workout_verify", "watch_list", "watch_remove")

    def workouts_page(request: Request, session: dict[str, Any] | None = None, *, confirm: str | None = None) -> Response:
        session = session or require_user(request)
        if isinstance(session, Response):
            return session
        with closing(connect(config.db_path)) as conn:
            data = pages.workouts_data(config, conn, today())
            job = queue.current(conn)
            view = agent_view(conn, job, request) if job and job.kind in WORKOUT_JOB_KINDS else None  # a strap pull is the Sync page's business
            online = queue.device_state(conn, now=now())["online"]
        q = request.query_params
        for push in data["history"]:
            push["when"] = local_time(push["when"], tz)
        return render(request, "workouts.html", session, {"page": "workouts", "d": data, "drafts": q.get("drafts"), "skipped": q.get("skipped"), "message": q.get("message"),
                                                          "confirm": confirm, "job": view, "busy": False, "poll_url": "/sync/job", "show_log": False, "offline": bool(view and view.active and not online), "error": q.get("error")})

    def enqueue_and_answer(request: Request, session: dict[str, Any], kind: str, params: dict[str, Any], back: str) -> Response:
        """Hand a workout job to the device agent; htmx gets the job partial, a plain post goes back to the page."""
        try:
            with closing(connect(config.db_path)) as conn:
                job = queue.enqueue(conn, kind, params, now=now())
        except ValueError as exc:
            return PlainTextResponse(str(exc), status_code=400)
        if is_htmx(request):
            return agent_partial(request, session, busy=job is None)
        return RedirectResponse(back, status_code=303)

    async def workout_push(request: Request) -> Response:
        """Push the ticked files (or the upcoming ones) to the watch through the agent; dry run and force as options."""
        got = await posted(request)
        if isinstance(got, Response):
            return got
        session, form = got
        flags = {k: "1" for k in ("dry_run", "force", "schedule") if form.get(k) is not None}
        if form.get("upcoming") is not None:
            return enqueue_and_answer(request, session, "workout_push_upcoming", {k: v for k, v in flags.items() if k != "schedule"}, "/workouts")
        names = [str(n) for n in form.getlist("names")]
        if not names:
            return RedirectResponse("/workouts?error=none", status_code=303) if not is_htmx(request) else PlainTextResponse("no workouts selected", status_code=400)
        return enqueue_and_answer(request, session, "workout_push", {"names": names, **flags}, "/workouts")

    async def workout_verify(request: Request) -> Response:
        got = await posted(request)
        if isinstance(got, Response):
            return got
        session, _form = got
        return enqueue_and_answer(request, session, "workout_verify", {}, "/workouts")

    async def workout_delete(request: Request) -> Response:
        """Two steps: the first post shows the confirmation, the second (with confirm=1) deletes the file."""
        got = await posted(request)
        if isinstance(got, Response):
            return got
        session, form = got
        from ..workouts.store import delete

        name = str(form.get("file") or "")
        if form.get("confirm") is None:
            return workouts_page(request, session, confirm=name)
        try:
            delete(config, name)
        except FileNotFoundError:
            return PlainTextResponse("unknown workout file", status_code=404)
        except ValueError:
            return PlainTextResponse("bad file name", status_code=400)
        return RedirectResponse("/workouts?message=deleted", status_code=303)

    def watch_page(request: Request, session: dict[str, Any] | None = None, *, confirm: str | None = None) -> Response:
        session = session or require_user(request)
        if isinstance(session, Response):
            return session
        with closing(connect(config.db_path)) as conn:
            data = pages.watch_data(conn, now=now())
            job = queue.current(conn)
            view = agent_view(conn, job, request) if job and job.kind in WORKOUT_JOB_KINDS else None
            state = queue.device_state(conn, now=now())
        if data["job"]:
            data["job"]["when"] = local_time(data["job"]["when"], tz)
        for item in data["items"]:
            item["created"] = local_time(item["created"], tz)
        return render(request, "workouts_watch.html", session, {"page": "workouts", "d": data, "confirm": confirm, "agent": state, "job": view, "busy": False, "poll_url": "/sync/job", "show_log": False,
                                                                "offline": bool(view and view.active and not state["online"])})

    async def watch_read(request: Request) -> Response:
        got = await posted(request)
        if isinstance(got, Response):
            return got
        session, _form = got
        return enqueue_and_answer(request, session, "watch_list", {}, "/workouts/watch")

    async def watch_remove(request: Request) -> Response:
        """Two steps as well: confirm, then the agent removes the file (after its backup) and re-reads the watch."""
        got = await posted(request)
        if isinstance(got, Response):
            return got
        session, form = got
        key = str(form.get("key") or "")
        if not key:
            return PlainTextResponse("no workout chosen", status_code=400)
        if form.get("confirm") is None:
            return watch_page(request, session, confirm=key)
        return enqueue_and_answer(request, session, "watch_remove", {"keys": [key]}, "/workouts/watch")

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

    async def set_language(request: Request) -> Response:
        """The navigation's language switch: a cookie for this browser (the TUI's F12), back to the page."""
        got = await posted(request)
        if isinstance(got, Response):
            return got
        _session, form = got
        code = str(form.get("lang") or "")
        target = str(form.get("next") or "/")
        if not target.startswith("/") or target.startswith("//"):
            target = "/"
        response = RedirectResponse(target, status_code=303)
        if code in UI_LANGUAGES:
            response.set_cookie(LANG_COOKIE, code, max_age=365 * 24 * 3600, httponly=True, secure=True, samesite="strict", path="/")
        return response

    # ---- backup and restore: the whole state as one archive (data, config, .env, certificates) ----------

    BACKUP_KEYS = ("backup-create", "backup-restore")

    def backup_paths() -> tuple[Path, Path | None, Path | None, Path | None]:
        """The backups folder, the config file, the .env file and the certificate folder as this process sees them."""
        from .. import backup as lib
        from ..runtime import certs_dir as find_certs
        from ..runtime import env_file as find_env

        return lib.backups_dir(config.data_dir), config.path or default_config_path(env), find_env(env), find_certs(env)

    def backup_page(request: Request, session: dict[str, Any] | None = None, *, confirm: str | None = None, preview: dict[str, Any] | None = None,
                    error: str | None = None, status_code: int = 200) -> Response:
        session = session or require_user(request)
        if isinstance(session, Response):
            return session
        data = pages.backup_data(config, dict(env), now=now())
        for item in data["archives"]:
            item["when"] = local_time(item["created_at"] or item["mtime"], tz)
        current = jobs.last_of(BACKUP_KEYS)
        view = web_view(current, request) if current else None
        context = {"page": "backup", "d": data, "confirm": confirm, "preview": preview, "error": error, "message": request.query_params.get("message"),
                   "job": view, "busy": False, "poll_url": "/backup/job", "show_log": True, "offline": False,
                   "restricted": bool(session.get("must_change")),  # the start-password session: upload and restore only
                   "restored": bool(current and current.key == "backup-restore" and current.status == "done")}
        return render(request, "backup.html", session, context, status_code=status_code)

    def backup_get(request: Request) -> Response:
        return backup_page(request)

    def backup_job(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        job = jobs.last_of(BACKUP_KEYS)
        return job_partial(request, session, keys=BACKUP_KEYS, poll_url="/backup/job", refresh=bool(request.query_params.get("poll") and job and not job.running))

    async def backup_create(request: Request) -> Response:
        got = await posted(request)
        if isinstance(got, Response):
            return got
        session, _form = got
        if session.get("must_change"):
            return PlainTextResponse("set a password first", status_code=403)
        folder, config_path, env_path, certs = backup_paths()
        env_values = dict(env)

        def run(conn: Any, log: Callable[[str], None]) -> str:
            from .. import backup as lib
            from ..runtime import host_path

            host_data = str(host_path(config.data_dir, env_values))
            stats = lib.create_backup(config.data_dir, config_path, env_path, folder, certs_dir=certs, now=datetime.now(tz),
                                      log=lambda line: log(line.replace(str(config.data_dir), host_data)))
            return f"{stats.manifest.files} files, {stats.manifest.bytes / 1e6:.1f} MB → {host_path(stats.path, env_values)}"

        started = jobs.start("backup-create", "Backup", run, cli="wristpull backup create")
        if is_htmx(request):
            return job_partial(request, session, keys=BACKUP_KEYS, poll_url="/backup/job", busy=started is None)
        return RedirectResponse("/backup", status_code=303)

    def backup_download(request: Request) -> Response:
        """One archive from <data_dir>/backups/ as an attachment (it holds secrets: the session guards it, like the page)."""
        from .. import backup as lib

        session = require_user(request)
        if isinstance(session, Response):
            return session
        if session.get("must_change"):
            return PlainTextResponse("set a password first", status_code=403)
        name = str(request.query_params.get("name") or "")
        folder = backup_paths()[0]
        if not lib.archive_name_ok(name) or not (folder / name).is_file():
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(str(folder / name), filename=name, media_type="application/gzip")

    async def backup_delete(request: Request) -> Response:
        """Two steps like a workout file: the first post shows the confirmation, the second (confirm=1) deletes."""
        from .. import backup as lib

        got = await posted(request)
        if isinstance(got, Response):
            return got
        session, form = got
        if session.get("must_change"):
            return PlainTextResponse("set a password first", status_code=403)
        name = str(form.get("file") or "")
        if not lib.archive_name_ok(name):
            return PlainTextResponse("bad archive name", status_code=400)
        if form.get("confirm") is None:
            return backup_page(request, session, confirm=name)
        try:
            lib.delete_backup(backup_paths()[0], name)
        except lib.BackupError:
            return PlainTextResponse("unknown archive", status_code=404)
        return RedirectResponse("/backup?message=deleted", status_code=303)

    async def backup_upload(request: Request) -> Response:
        """The raw request body (an archive, hundreds of MB) streamed into <data_dir>/backups/: no form parser, no
        temp copy; the CSRF token comes in the header, the file name in X-File-Name. The archive is checked
        (manifest) before it is listed; anything else is removed again."""
        import shutil

        from .. import backup as lib

        session = session_of(request)
        if not session or not session.get("user"):
            return PlainTextResponse("log in first", status_code=401)
        if session.get("must_change") and current_hash() is not None:
            return restored_logout(request)
        if not csrf_ok(request, session, None):
            return PlainTextResponse("CSRF token missing or wrong", status_code=403)
        length = request.headers.get("content-length") or ""
        if not length.isdigit() or int(length) <= 0:
            return PlainTextResponse("Content-Length required", status_code=411)
        size = int(length)
        folder = backup_paths()[0]
        folder.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(folder).free
        if size > free - UPLOAD_CHUNK * 64:
            return PlainTextResponse(f"not enough free space: {free / 1e6:.0f} MB left", status_code=413)
        target = lib.upload_target(folder, unquote(request.headers.get("x-file-name") or ""), now())
        part = target.with_name(target.name + lib.PART_SUFFIX)
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        written = 0
        try:
            with os.fdopen(fd, "wb") as fh:
                async for chunk in request.stream():
                    fh.write(chunk)
                    written += len(chunk)
            if written != size:
                return PlainTextResponse(f"incomplete upload: {written} of {size} bytes", status_code=400)
            os.replace(part, target)
            try:
                lib.read_manifest(target)
            except lib.BackupError as exc:
                target.unlink(missing_ok=True)
                return PlainTextResponse(str(exc), status_code=400)
        finally:
            part.unlink(missing_ok=True)
        return PlainTextResponse(target.name, status_code=201)

    async def backup_restore(request: Request) -> Response:
        """Step one shows what the archive holds and what this instance already has; step two (confirm=1) needs the
        password and starts the restore job. Nothing is deleted: the archive's files overwrite, the database is
        copied into the live file, config and .env are rewritten in place."""
        from .. import backup as lib

        got = await posted(request)
        if isinstance(got, Response):
            return got
        session, form = got
        name = str(form.get("file") or "")
        preview = pages.backup_preview(config, dict(env), name)
        if preview is None:
            return PlainTextResponse("unknown archive", status_code=404)
        if form.get("confirm") is None:
            return backup_page(request, session, preview=preview)
        if preview.get("error"):
            return backup_page(request, session, preview=preview, error="archive", status_code=400)
        encoded = current_hash()
        given = str(form.get("password") or "")
        if not ((encoded is None and given == INITIAL_PASSWORD) or (encoded and verify_password(given, encoded))):
            return backup_page(request, session, preview=preview, error="password", status_code=403)
        if not preview["enough_space"]:
            return backup_page(request, session, preview=preview, error="space", status_code=400)
        running = jobs.current()
        if running and running.running:
            return backup_page(request, session, preview=preview, error="busy", status_code=409)
        with closing(connect(config.db_path)) as conn:
            if queue.current_active(conn):
                return backup_page(request, session, preview=preview, error="agent", status_code=409)
        folder, config_path, env_path, certs = backup_paths()
        env_values = dict(env)

        def run(conn: Any, log: Callable[[str], None]) -> str:
            from ..config import load_config
            from ..runtime import HOST_CONFIG_ENV, HOST_DATA_DIR_ENV

            stats = lib.restore_backup(
                folder / name,
                data_dir=config.data_dir,
                config_path=config_path,
                env_path=env_path,
                certs_dir=certs,
                host_data_dir=env_values.get(HOST_DATA_DIR_ENV) or None,
                host_config=env_values.get(HOST_CONFIG_ENV) or None,
                force=True,
                log=log,
            )
            if stats.config_path:
                fresh = load_config(stats.config_path, env=env_values)
                fresh.data_dir = config.data_dir  # the mount (or the local folder) stays where it is
                pages.apply_config(config, fresh)
                log("configuration reloaded")
            store.apply(config)
            parts = [f"{stats.files} files restored"]
            if stats.db_replaced_live:
                parts.append("database overwritten in place")
            if stats.env_path:
                parts.append(".env rewritten")
            if stats.certs_dir:
                parts.append("certificates rewritten")
            for note in stats.notes:
                log(f"note: {note}")
            return ", ".join(parts)

        started = jobs.start("backup-restore", "Restore from backup", run, cli=f"wristpull backup restore {folder / name} --force")
        if is_htmx(request):
            return job_partial(request, session, keys=BACKUP_KEYS, poll_url="/backup/job", busy=started is None)
        return RedirectResponse("/backup", status_code=303)

    async def backup_restart(request: Request) -> Response:
        """Stop the server so Docker starts it again with the restored secrets store and config (only inside the
        container; a native `wristpull serve` has no supervisor and gets instructions instead)."""
        from ..runtime import in_container

        got = await posted(request)
        if isinstance(got, Response):
            return got
        session, _form = got
        if not in_container(env):
            return PlainTextResponse("not running in a container", status_code=404)
        response = render(request, "restarting.html", session, {"page": "backup"})
        (deps.restart or restart_process)()
        return response

    def help_page(request: Request) -> Response:
        session = require_user(request)
        if isinstance(session, Response):
            return session
        return render(request, "help.html", session, {"page": "help", "d": pages.help_data(config, dict(env))})

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
            Route("/insights", insights_page),
            Route("/report", report_page),
            Route("/tags", tags_post, methods=["POST"]),
            Route("/days", days_page),
            Route("/days/detail", day_detail),
            Route("/files", files_page),
            Route("/files/download", files_download),
            Route("/export", export_page),
            Route("/export/run", export_run, methods=["POST"]),
            Route("/export/job", export_job),
            Route("/settings", settings_get),
            Route("/settings/config", settings_config, methods=["POST"]),
            Route("/settings/secrets", settings_secrets, methods=["POST"]),
            Route("/settings/password", password_get),
            Route("/settings/password", password_post, methods=["POST"]),
            Route("/settings/check-coach", settings_check, methods=["POST"]),
            Route("/settings/job", settings_job),
            Route("/lang", set_language, methods=["POST"]),
            Route("/coach/profile", coach_profile_get),
            Route("/coach/profile", coach_profile_post, methods=["POST"]),
            Route("/coach/wizard", coach_wizard_get),
            Route("/coach/wizard", coach_wizard_post, methods=["POST"]),
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
            Route("/workouts/push", workout_push, methods=["POST"]),
            Route("/workouts/verify", workout_verify, methods=["POST"]),
            Route("/workouts/delete", workout_delete, methods=["POST"]),
            Route("/workouts/watch", watch_page),
            Route("/workouts/watch/read", watch_read, methods=["POST"]),
            Route("/workouts/watch/remove", watch_remove, methods=["POST"]),
            Route("/workouts/drafts", workout_drafts, methods=["POST"]),
            Route("/workouts/edit", workout_edit),
            Route("/workouts/check", workout_check, methods=["POST"]),
            Route("/workouts/save", workout_save, methods=["POST"]),
            Route("/backup", backup_get),
            Route("/backup/create", backup_create, methods=["POST"]),
            Route("/backup/job", backup_job),
            Route("/backup/download", backup_download),
            Route("/backup/delete", backup_delete, methods=["POST"]),
            Route("/backup/upload", backup_upload, methods=["POST"]),
            Route("/backup/restore", backup_restore, methods=["POST"]),
            Route("/backup/restart", backup_restart, methods=["POST"]),
            Route("/login", login_page, methods=["GET"]),
            Route("/login", login_submit, methods=["POST"]),
            Route("/logout", logout, methods=["POST"]),
            Route("/help", help_page),
            Route("/healthz", healthz),
            Mount("/static", app=StaticFiles(directory=str(HERE / "static")), name="static"),
        ],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=headers)],
    )
    return app
