from pathlib import Path

import pytest

textual = pytest.importorskip("textual")

from textual.widgets import DataTable  # noqa: E402

from tui_helpers import make_app, make_deps, run  # noqa: E402
from wristpull.config import Config  # noqa: E402
from wristpull.tui.screens.days import DaysScreen  # noqa: E402
from wristpull.tui.screens.files import FilesScreen  # noqa: E402
from wristpull.tui.screens.help import HelpScreen  # noqa: E402
from wristpull.tui.screens.home import HomeScreen  # noqa: E402
from wristpull.tui.screens.settings import SettingsScreen  # noqa: E402
from wristpull.tui.screens.sync import SyncScreen  # noqa: E402
from wristpull.tui.widgets import KeyBar, Panel  # noqa: E402


def test_start_navigate_and_switch_language(config: Config, tmp_path: Path):
    config.ui_language = "de"
    app = make_app(config, make_deps(tmp_path))

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, HomeScreen)
            assert app.sub_title == "Übersicht"
            assert "Beenden" in app.screen.query_one(KeyBar).content
            for key, cls in (("2", SyncScreen), ("3", DaysScreen), ("6", FilesScreen), ("8", SettingsScreen), ("1", HomeScreen)):
                await pilot.press(key)
                await pilot.pause()
                assert isinstance(app.screen, cls), key
            await pilot.press("question_mark")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, HomeScreen)
            await pilot.press("f12")
            await pilot.pause()
            assert app.language == "en"
            assert app.sub_title == "Overview"

    run(scenario())


def test_home_shows_status_without_data(config: Config, tmp_path: Path):
    app = make_app(config, make_deps(tmp_path))

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            devices = str(app.screen.query_one("#devices", Panel).content)
            assert "not connected" in devices
            notes = str(app.screen.query_one("#notes", Panel).content)
            assert "not yet verified" in notes and "No sleep data" in notes
            assert app.screen.query_one("#recent", DataTable).row_count == 0

    run(scenario())


def test_days_screen_periods_and_detail(config: Config, tmp_path: Path, garmin_tree: Path):
    from wristpull.importers.garmin import import_garmin

    app = make_app(config, make_deps(tmp_path, garmin_tree))
    import_garmin(app.conn, config, path=garmin_tree)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("3")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, DaysScreen)
            table = screen.query_one("#daytable", DataTable)
            assert table.row_count >= 1
            detail = str(screen.query_one("#detail", Panel).content)
            assert "Steps" in detail and "Garmin" in detail
            await pilot.press("w")
            await pilot.pause()
            assert (screen.period_end - screen.period_start).days == 6
            await pilot.press("left")
            await pilot.pause()
            assert screen.period_end < screen.period_start + __import__("datetime").timedelta(days=7)
            await pilot.press("m")
            await pilot.pause()
            assert screen.period_start.day == 1
            await pilot.press("d")
            await pilot.pause()
            assert (screen.period_end - screen.period_start).days == config.ui_range_days - 1

    run(scenario())


def test_files_screen_inspects_fit_and_opens(config: Config, tmp_path: Path, garmin_tree: Path):
    from wristpull.importers.garmin import import_garmin

    deps = make_deps(tmp_path, garmin_tree)
    app = make_app(config, deps)
    import_garmin(app.conn, config, path=garmin_tree)
    archived = next((config.raw_dir / "garmin_usb").rglob("*.fit"), None) or next((config.raw_dir / "garmin_usb").rglob("*.FIT"))
    (config.data_dir / "exports").mkdir()
    (config.data_dir / "exports" / "summary.md").write_text("# hello\n", "utf-8")

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("6")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, FilesScreen)
            screen.show_file(archived)
            info = str(screen.query_one("#file-info", Panel).content)
            assert "garmin_usb" in info and "Parse status ok" in info
            inspect = str(screen.query_one("#file-inspect", Panel).content)
            assert "file_id" in inspect
            screen.show_file(config.data_dir / "exports" / "summary.md")
            assert "# hello" in str(screen.query_one("#file-inspect", Panel).content)
            await pilot.press("o")
            await pilot.pause()
            assert deps.opened == [config.data_dir / "exports" / "summary.md"]

    run(scenario())


def test_config_screen_renders_values(config: Config, tmp_path: Path):
    config.ui_language = "de"
    app = make_app(config, make_deps(tmp_path))

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("8")
            await pilot.pause()
            text = str(app.screen.query_one("#settings-preview", Panel).content)
            assert "[ui]" in text and 'language = "de"' in text

    run(scenario())
