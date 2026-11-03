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
from datetime import date, datetime
from pathlib import Path
from typing import Any
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

from ..config import Config
from ..db import connect
from . import pages
from .auth import RateLimiter, SessionCodec, new_session, verify_password
from .i18n import pick_language, translations

HERE = Path(__file__).parent
COOKIE = "wristpull_session"
CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"


@dataclass
class WebDeps:
    """Side effects the tests replace: the clock and the login limiter."""

    today: Callable[[], date] | None = None  # None: today in the configured time zone
    limiter: RateLimiter = field(default_factory=RateLimiter)


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


def create_app(config: Config, *, password_hash: str, secret: str, deps: WebDeps | None = None) -> Starlette:
    deps = deps or WebDeps()
    codec = SessionCodec(secret)
    tz = ZoneInfo(config.timezone)
    templates = Jinja2Templates(directory=str(HERE / "templates"))

    @pass_context
    def num(ctx: Any, value: float | None, digits: int = 0) -> str:
        return _number(value, digits, ctx.get("lang", "en"))

    @pass_context
    def km(ctx: Any, metres: float | None) -> str:
        return format_km(metres, ctx.get("lang", "en"))

    templates.env.filters["num"] = num
    templates.env.filters["km"] = km
    templates.env.filters["dur"] = format_duration
    templates.env.filters["hm"] = format_hm
    templates.env.globals["pace"] = format_pace

    def today() -> date:
        return deps.today() if deps.today else datetime.now(tz).date()

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

    def require_user(request: Request) -> dict[str, Any] | Response:
        session = session_of(request)
        if not session or not session.get("user"):
            return RedirectResponse("/login", status_code=303)
        return session

    def client_key(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    # ---- routes ----------------------------------------------------------------------

    def login_page(request: Request) -> Response:
        session = session_of(request)
        if session and session.get("user"):
            return RedirectResponse("/", status_code=303)
        session = session or new_session()
        return render(request, "login.html", session, {"error": None}, set_session=True)

    async def login_submit(request: Request) -> Response:
        form = await request.form()
        session = session_of(request)
        if not csrf_ok(request, session, form):
            return PlainTextResponse("CSRF token missing or wrong", status_code=403)
        assert session is not None
        client = client_key(request)
        if deps.limiter.blocked(client):
            return render(request, "login.html", session, {"error": "blocked"}, status_code=429)
        if not verify_password(str(form.get("password") or ""), password_hash):
            deps.limiter.failure(client)
            return render(request, "login.html", session, {"error": "wrong"}, status_code=401)
        deps.limiter.success(client)
        response = RedirectResponse("/", status_code=303)
        set_cookie(response, new_session(user=True))  # a new CSRF token after login
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
            data = pages.today_data(conn, today())
        for tile in data["tiles"]:
            tile["text"] = format_value(tile["value"], tile["kind"], lang)
            tile["delta_text"] = format_delta(tile["delta_28d"], tile["kind"], lang)
            tile["mean_text"] = format_value(tile["mean_28d"], tile["kind"], lang)
        imports = {source: local_time(stamp, tz) for source, stamp in data["imports"].items()}
        coach_time = local_time(data["coach"]["created_at_utc"], tz) if data["coach"] else None
        return render(request, "today.html", session, {"page": "today", "d": data, "imports": imports, "coach_time": coach_time, "today": today(), "source_labels": pages.SOURCE_LABELS})

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
        charts_json = chart_json(data["charts"], lang)  # "<" escaped: no "</script>" inside the data block
        return render(request, "trends.html", session, {"page": "trends", "d": data, "charts_json": charts_json, "choices": pages.TREND_WEEKS})

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
            Route("/activities", activities_page),
            Route("/activities/{activity_id:int}", activity_page),
            Route("/sleep", sleep_page),
            Route("/sleep/{sleep_id:int}", night_page),
            Route("/login", login_page, methods=["GET"]),
            Route("/login", login_submit, methods=["POST"]),
            Route("/logout", logout, methods=["POST"]),
            Route("/healthz", healthz),
            Mount("/static", app=StaticFiles(directory=str(HERE / "static")), name="static"),
        ],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=headers)],
    )
    return app
