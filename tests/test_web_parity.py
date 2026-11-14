"""Every TUI screen and every job the TUI can start has a counterpart in the web dashboard (the parity rule)."""

from pathlib import Path

import pytest

pytest.importorskip("starlette")

from wristpull.agent.kinds import KINDS  # noqa: E402
from wristpull.config import Config  # noqa: E402
from wristpull.tui.nav import NAV  # noqa: E402
from wristpull.web import pages  # noqa: E402
from wristpull.web.app import create_app  # noqa: E402

WEB_DIR = Path(__file__).resolve().parents[1] / "src" / "wristpull" / "web"
# TUI screen name -> the web page that covers it
SCREEN_PAGES = {
    "home": "/",
    "sync": "/sync",
    "days": "/days",
    "activities": "/activities",
    "sleep": "/sleep",
    "files": "/files",
    "export": "/export",
    "config": "/settings",
    "coach": "/coach",
    "workouts": "/workouts",
}
# in-process jobs the TUI runs (tui/jobs.py factories) -> the web route that starts them
IN_PROCESS_JOBS = {"export": "/export/run", "report": "/export/run", "coach": "/coach/run", "coach_check": "/settings/check-coach"}


def routes(config: Config) -> set[str]:
    app = create_app(config, password_hash=None, secret="s" * 32)
    return {getattr(r, "path", "") for r in app.routes}


def test_every_tui_screen_has_a_web_page(config: Config):
    paths = routes(config)
    for _key, name, _label in NAV:
        assert name in SCREEN_PAGES, f"TUI screen {name} has no web page in the parity map"
        assert SCREEN_PAGES[name] in paths, f"{SCREEN_PAGES[name]} is not a route"
    assert len(NAV) == 10


def test_every_agent_job_kind_is_started_from_the_dashboard():
    html = "\n".join(p.read_text("utf-8") for p in WEB_DIR.rglob("*.html"))
    app_py = (WEB_DIR / "app.py").read_text("utf-8")
    for kind in KINDS:
        assert f'value="{kind}"' in html or f'"{kind}"' in app_py, f"agent job kind {kind} is not started by any page"


def test_in_process_jobs_and_help_cover_the_tui(config: Config):
    paths = routes(config)
    for job, route in IN_PROCESS_JOBS.items():
        assert route in paths, f"{job}: {route} missing"
    assert "/help" in paths and "/coach/profile" in paths and "/coach/wizard" in paths and "/workouts/watch" in paths and "/lang" in paths
    help_paths = {p[2] for p in pages.HELP_PAGES}
    assert set(SCREEN_PAGES.values()) <= help_paths and {"/trends", "/insights"} <= help_paths
    keys = {k for k, _s, _p, _w, _c in pages.HELP_PAGES if k != "–"}
    assert keys == {key for key, _n, _l in NAV}
