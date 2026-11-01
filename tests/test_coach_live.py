"""Opt-in live test against a real backend: WRISTPULL_LIVE=1 and the backend's key (or a running Ollama).

Skipped by default so the suite never touches the network. Run with e.g.
    WRISTPULL_LIVE=1 WRISTPULL_LIVE_BACKEND=openrouter WRISTPULL_LIVE_MODEL=deepseek/deepseek-v4.1-flash-1017 uv run pytest tests/test_coach_live.py -s
"""

import os
from datetime import date
from pathlib import Path

import pytest

from wristpull.coach.backends import make_backend
from wristpull.coach.profile import profile_path
from wristpull.coach.runner import run_coach
from wristpull.config import Config
from wristpull.importers.garmin import import_garmin

pytestmark = pytest.mark.skipif(os.environ.get("WRISTPULL_LIVE") != "1", reason="set WRISTPULL_LIVE=1 to call a real LLM backend")


def test_live_daily_check(conn, config: Config, garmin_tree: Path):
    backend_name = os.environ.get("WRISTPULL_LIVE_BACKEND", "ollama")
    model = os.environ.get("WRISTPULL_LIVE_MODEL") or ("gemma3:4b" if backend_name == "ollama" else None)
    config.coach.backend = backend_name
    if model:
        config.coach.model = model
    config.coach.max_tokens = 800
    import_garmin(conn, config, path=garmin_tree)
    path = profile_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Goal profile\n- Goals: general fitness\n- Constraints: none\n", "utf-8")
    backend = make_backend(config)
    print("\n", backend.check())
    result = run_coach(conn, config, "daily", today=date(2026, 10, 6), backend=backend, log=print)
    print(result.answer_md)
    assert result.status == "ok" and result.sections.get("suggestion") and result.sections.get("numbers")
