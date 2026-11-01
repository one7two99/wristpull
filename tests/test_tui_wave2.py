from datetime import UTC, datetime
from pathlib import Path

import pytest

textual = pytest.importorskip("textual")

from textual.widgets import DataTable, Input, Select  # noqa: E402

from gb_db import build_gb_db  # noqa: E402
from tui_helpers import make_app, make_deps, run, wait_jobs  # noqa: E402
from wristpull.config import Config, load_config  # noqa: E402
from wristpull.importers.garmin import import_garmin  # noqa: E402
from wristpull.importers.phone import import_gadgetbridge_db  # noqa: E402
from wristpull.tui.format import bar, stage_bar  # noqa: E402
from wristpull.tui.jobs import JobSpec  # noqa: E402
from wristpull.tui.screens.activities import ActivitiesScreen, pace_per_km  # noqa: E402
from wristpull.tui.screens.export import ExportScreen  # noqa: E402
from wristpull.tui.screens.settings import SettingsScreen  # noqa: E402
from wristpull.tui.screens.sleep import SleepScreen  # noqa: E402
from wristpull.tui.widgets import Panel  # noqa: E402


@pytest.fixture
def loaded_app(config: Config, tmp_path: Path, garmin_tree: Path):
    app = make_app(config, make_deps(tmp_path, garmin_tree))
    import_garmin(app.conn, config, path=garmin_tree)
    import_gadgetbridge_db(app.conn, config, build_gb_db(tmp_path / "Gadgetbridge.db"))
    return app


def test_activities_screen_filter_detail_and_open(loaded_app):
    app = loaded_app

    async def scenario():
        async with app.run_test(size=(130, 42)) as pilot:
            await pilot.press("4")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ActivitiesScreen)
            await pilot.press("m")  # this month (today is 2026-10-06 in tests): the Garmin run and the strap walk
            await pilot.pause()
            table = screen.query_one("#acttable", DataTable)
            assert table.row_count == 2
            await pilot.press("w")  # this week: only the Garmin run on 2026-10-06
            await pilot.pause()
            assert table.row_count == 1
            detail = str(screen.query_one("#actdetail", Panel).content)
            assert "running" in detail and "Z1" in detail and "Lap" in detail and "garmin_usb/" in detail
            await pilot.press("m")
            await pilot.pause()
            screen.query_one("#filter", Input).value = "evening"
            await pilot.pause()
            assert table.row_count == 1
            assert "Evening walk" in str(screen.query_one("#actdetail", Panel).content)
            await pilot.press("o")  # the strap workout's "raw file" is the Gadgetbridge database
            await pilot.pause()
            assert app.deps.opened[-1].suffix == ".db"
            screen.query_one("#filter", Input).value = "run"
            await pilot.pause()
            await pilot.press("o")
            await pilot.pause()
            assert app.deps.opened[-1].suffix.lower() == ".fit"

    run(scenario())


def test_sleep_screen_rows_detail_and_source_toggle(loaded_app):
    app = loaded_app

    async def scenario():
        async with app.run_test(size=(130, 42)) as pilot:
            await pilot.press("5")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, SleepScreen)
            screen.period_start, screen.period_end = __import__("datetime").date(2026, 10, 1), __import__("datetime").date(2026, 11, 2)
            screen.refresh_data()
            table = screen.query_one("#nights", DataTable)
            assert table.row_count == 2  # Garmin night + strap night
            detail = str(screen.query_one("#nightdetail", Panel).content)
            assert "▒" in detail and "Score" in detail and "HRV" in detail
            await pilot.press("g")  # garmin only
            await pilot.pause()
            assert table.row_count == 1 and "garmin" in str(screen.query_one("#period", Panel).content)
            await pilot.press("g")  # strap only
            await pilot.pause()
            assert table.row_count == 1 and "gadgetbridge_zepp" in str(screen.query_one("#nightdetail", Panel).content)

    run(scenario())


def test_export_screen_runs_export_and_report(loaded_app, config: Config):
    app = loaded_app

    async def scenario():
        async with app.run_test(size=(130, 42)) as pilot:
            await pilot.press("7")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ExportScreen)
            screen.query_one("#exp-from", Input).value = "2026-10-01"
            screen.query_one("#exp-to", Input).value = "2026-10-07"
            screen.action_run()
            await wait_jobs(app)
            await pilot.pause()
            result = str(screen.query_one("#export-result", Panel).content)
            assert "summary.md" in result and "wristpull export --for-claude --from 2026-10-01 --to 2026-10-07" in result
            assert (config.data_dir / "exports" / "2026-10-01_2026-10-07" / "summary.md").exists()
            assert "#" in str(screen.query_one("#export-preview", Panel).content)
            await pilot.click("#kind-week")
            await pilot.pause()
            assert screen._kind() == "week"
            screen.action_run()
            await wait_jobs(app)
            await pilot.pause()
            result = str(screen.query_one("#export-result", Panel).content)
            assert "report.md" in result and "wristpull report week --date 2026-10-07 --charts" in result
            await pilot.press("o")
            await pilot.pause()
            assert app.deps.opened[-1].name.startswith("week-")
            screen.query_one("#exp-to", Input).value = "nonsense"
            assert screen.build_spec() is None

    run(scenario())


def test_settings_screen_saves_and_switches_language(config: Config, tmp_path: Path):
    config.path = None
    app = make_app(config, make_deps(tmp_path))
    target = tmp_path / "cfg" / "config.toml"

    async def scenario():
        async with app.run_test(size=(130, 42)) as pilot:
            await pilot.press("8")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, SettingsScreen)
            app.config.path = target  # where the file goes
            screen.query_one("#set-timezone", Input).value = "Mars/Olympus"
            screen.action_save()
            await pilot.pause()
            assert not target.exists() and "timezone" in str(screen.query_one("#settings-status", Panel).content)
            screen.query_one("#set-timezone", Input).value = "Europe/Vienna"
            screen.query_one("#set-range", Input).value = "14"
            screen.query_one("#set-language", Select).value = "de"
            screen.action_save()
            await pilot.pause()
            assert target.exists()
            loaded = load_config(target)
            assert loaded.timezone == "Europe/Vienna" and loaded.ui_range_days == 14 and loaded.ui_language == "de"
            assert app.config.timezone == "Europe/Vienna" and app.language == "de"
            assert isinstance(app.screen, SettingsScreen) and app.sub_title == "Einstellungen"

    run(scenario())


def test_cancel_running_job(config: Config, tmp_path: Path):
    import time

    app = make_app(config, make_deps(tmp_path))

    def slow(conn, log):
        for _ in range(500):
            if app.runner.should_stop():
                log("stopping")
                return "stopped"
            time.sleep(0.01)
        return "finished"

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("2")
            await pilot.pause()
            assert app.start_job(JobSpec("slow", "Slow", "slow", slow))
            await pilot.pause(0.05)
            await pilot.press("x")
            await wait_jobs(app)
            await pilot.pause()
            record = app.runner.last
            assert record is not None and record.result == "stopped" and record.cancelled
            assert any("stop requested" in line for line in app.runner.lines)
            assert any("⏹" in line for line in record.lines)
            app.action_cancel_job()  # nothing running: just a notification, no crash

    run(scenario())


def test_format_helpers_for_wave2():
    base = datetime(2026, 10, 5, 22, 0, tzinfo=UTC)
    stages = [(base, base.replace(hour=23), "light"), (base.replace(hour=23), base.replace(hour=23, minute=30), "deep")]
    assert stage_bar(stages, base, base.replace(hour=23, minute=30), 6) == "▒▒▒▒▓▓"
    assert stage_bar([], base, base, 5) == ""
    assert bar(30, 60, 10) == "█████" and bar(None, 60) == "" and bar(1, 1000, 10) == "█"
    assert pace_per_km(5000, 1500) == "5:00 /km" and pace_per_km(None, 100) == "–"


def test_settings_screen_edits_coach_section(config: Config, tmp_path: Path):
    from wristpull.coach.backends import FakeBackend

    config.path = None
    deps = make_deps(tmp_path)
    deps.coach_backend = lambda cfg, **kw: FakeBackend(model=cfg.coach.model)
    app = make_app(config, deps)
    target = tmp_path / "cfg" / "config.toml"

    async def scenario():
        async with app.run_test(size=(130, 50)) as pilot:
            await pilot.press("8")
            await pilot.pause()
            screen = app.screen
            app.config.path = target
            screen.query_one("#set-coach-backend", Select).value = "claude"
            screen.query_one("#set-coach-model", Input).value = "claude-opus-5-5"
            screen.query_one("#set-coach-weekly-model", Input).value = "claude-sonnet-5-5"
            screen.query_one("#set-coach-key-env", Input).value = "MY_KEY"
            screen.query_one("#set-coach-budget", Input).value = "12000"
            screen.query_one("#set-coach-effort", Select).value = "high"
            screen.query_one("#set-coach-language", Select).value = "de"
            screen.query_one("#set-coach-max-tokens", Input).value = "50"  # too small: refused
            screen.action_save()
            await pilot.pause()
            assert not target.exists() and "max_tokens" in str(screen.query_one("#settings-status", Panel).content)
            screen.query_one("#set-coach-max-tokens", Input).value = "1500"
            screen.action_save()
            await pilot.pause()
            loaded = load_config(target).coach
            assert (loaded.backend, loaded.model, loaded.weekly_model, loaded.api_key_env, loaded.token_budget, loaded.effort, loaded.language, loaded.max_tokens) == ("claude", "claude-opus-5-5", "claude-sonnet-5-5", "MY_KEY", 12000, "high", "de", 1500)
            assert app.config.coach.backend == "claude"
            # the connection check runs as a job and reports into the status panel
            screen.action_check_coach()
            await wait_jobs(app)
            await pilot.pause()
            assert "fake backend ready" in str(screen.query_one("#settings-status", Panel).content)

    run(scenario())
