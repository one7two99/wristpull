from datetime import UTC, datetime
from pathlib import Path

import pytest

textual = pytest.importorskip("textual")

from conftest import build_monitor_fit  # noqa: E402
from gb_db import build_gb_db  # noqa: E402
from tui_helpers import TODAY, make_app, make_deps, run, wait_jobs  # noqa: E402
from wristpull.config import Config  # noqa: E402
from wristpull.importers.garmin import ImportStats  # noqa: E402
from wristpull.importers.phone import GB_FILES_DIR, PullStats  # noqa: E402
from wristpull.tui.jobs import JobRecord, JobSpec, describe_result, export_job, garmin_import_job, last_days, phone_pull_job, report_job  # noqa: E402
from wristpull.tui.screens.sync import SyncScreen  # noqa: E402
from wristpull.tui.widgets import JobLog, Panel  # noqa: E402


@pytest.fixture
def phone_root(tmp_path: Path) -> Path:
    root = tmp_path / "phone"
    (root / "sdcard/Gadgetbridge").mkdir(parents=True)
    build_gb_db(root / "sdcard/Gadgetbridge/Gadgetbridge.db")
    mon = root / GB_FILES_DIR.lstrip("/") / "AA:BB:CC:DD:EE:01" / "MONITOR/2026"
    mon.mkdir(parents=True)
    (mon / "MONITOR_2026-10-05.fit").write_bytes(build_monitor_fit(datetime(2026, 10, 4, 23, 0, tzinfo=UTC)))
    return root


def test_import_job_from_home_and_refused_while_busy(config: Config, tmp_path: Path, garmin_tree: Path):
    app = make_app(config, make_deps(tmp_path, garmin_tree))

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("i")
            await wait_jobs(app)
            await pilot.pause()
            record = app.runner.last
            assert record is not None and record.ok
            assert isinstance(record.result, ImportStats) and record.result.new == 5
            assert any("5 FIT files, 5 new" in line for line in record.lines)
            assert any(line.startswith("▶") and "wristpull import garmin" in line for line in app.runner.lines)
            # the overview picked up the data
            store = str(app.screen.query_one("#store", Panel).content)
            assert "garmin_usb" in store
            # a second job is refused while one is marked active
            app.runner.active = JobSpec("x", "busy", "cli", lambda conn, log: None)
            assert app.start_job(garmin_import_job(config, app.deps)) is False
            app.runner.active = None

    run(scenario())


def test_pull_job_from_sync_screen_logs_and_shows_result(config: Config, tmp_path: Path, phone_root: Path):
    app = make_app(config, make_deps(tmp_path, phone_root=phone_root))

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("2")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, SyncScreen)
            screen.query_one("#phone-trigger").value = False
            await pilot.press("p")
            await wait_jobs(app)
            await pilot.pause()
            record = app.runner.last
            assert record is not None and record.ok, record.error
            assert isinstance(record.result, PullStats)
            assert record.result.db_new and record.result.fit_new == 1
            assert record.spec.cli == "wristpull pull phone --no-trigger"
            result = str(screen.query_one("#result", Panel).content)
            assert "new database export imported" in result and "Amazfit Helio Strap" in result
            log = screen.query_one("#log", JobLog)
            assert len(log.lines) > 2

    run(scenario())


def test_failing_job_is_reported_not_raised(config: Config, tmp_path: Path):
    from fake_adb import FakeAdb

    deps = make_deps(tmp_path)  # no garmin roots
    deps.adb_factory = lambda: FakeAdb(tmp_path / "phone", devices=[])  # no phone -> NoDeviceError
    app = make_app(config, deps)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.start_job(phone_pull_job(config, app.deps, trigger=False))
            await wait_jobs(app)
            await pilot.pause()
            record = app.runner.last
            assert record is not None and not record.ok
            assert "✗" in record.lines[0]
            app.start_job(garmin_import_job(config, app.deps))
            await wait_jobs(app)
            await pilot.pause()
            record = app.runner.last
            assert record.ok and record.result.root is None
            assert "No Garmin device found" in record.lines[0]

    run(scenario())


def test_export_and_report_jobs(config: Config, tmp_path: Path, garmin_tree: Path):
    from wristpull.importers.garmin import import_garmin

    app = make_app(config, make_deps(tmp_path, garmin_tree))
    import_garmin(app.conn, config, path=garmin_tree)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("e")
            await wait_jobs(app)
            await pilot.pause()
            assert app.runner.last.ok and (config.data_dir / "exports").exists()
            assert "wrote" in app.runner.last.lines[0]
            await pilot.press("r")
            await wait_jobs(app)
            await pilot.pause()
            assert app.runner.last.ok and app.runner.last.spec.key == "report_week"
            assert any(p.name == "report.md" for p in app.runner.last.result.files)

    run(scenario())


def test_job_factories_build_cli_strings(config: Config, tmp_path: Path):
    deps = make_deps(tmp_path)
    assert garmin_import_job(config, deps).cli == "wristpull import garmin"
    assert garmin_import_job(config, deps, path=Path("/x/GARMIN"), dry_run=True).cli == "wristpull import garmin --path /x/GARMIN --dry-run"
    assert phone_pull_job(config, deps, serial="ABC", sync=False, wait=90, dry_run=True).cli == "wristpull pull phone --serial ABC --no-sync --wait 90 --dry-run"
    start, end = last_days(TODAY, 7)
    assert (start.isoformat(), end.isoformat()) == ("2026-09-30", "2026-10-06")
    assert export_job(config, start, end).cli == "wristpull export --for-claude --from 2026-09-30 --to 2026-10-06"
    assert report_job(config, "month", TODAY, charts=False).cli == "wristpull report month --date 2026-10-06"


def test_describe_result_variants():
    spec = JobSpec("k", "T", "cli", lambda c, l: None)
    assert describe_result(JobRecord(spec, error=RuntimeError("boom")))[0] == "✗ T: boom"
    stats = ImportStats(root=Path("/g"), found=3, new=1, skipped=2, rows={"steps": 10})
    lines = describe_result(JobRecord(spec, result=stats))
    assert lines[0] == "✓ 3 FIT files, 1 new, 2 already archived, 0 errors" and "steps 10" in lines[1]
    pull = PullStats(device="X", db_pulled=True, db_new=False, db_devices=["d1"], messages=["note"])
    lines = describe_result(JobRecord(spec, result=pull))
    assert "database unchanged" in lines[0] and lines[1] == "  device: d1" and lines[2] == "  ! note"
    assert describe_result(JobRecord(spec, result=object()))[0] == "✓ done"


def test_strap_pull_job_runs_against_a_fake_band(config: Config, tmp_path: Path, monkeypatch):
    from datetime import UTC, datetime

    from fake_band import FAKE_ADDRESS, FAKE_KEY, FakeBandState, FakeTransport
    from zepp_buffers import stress_minutes
    from wristpull.tui.jobs import strap_pull_job

    config.strap_address = FAKE_ADDRESS
    monkeypatch.setenv("WRISTPULL_STRAP_AUTHKEY", FAKE_KEY.hex())
    state = FakeBandState()
    state.minutes["stress"] = (datetime(2026, 11, 6, 23, 0, tzinfo=UTC), stress_minutes([20, None, 35]))
    deps = make_deps(tmp_path)
    deps.transport_factory = lambda address: FakeTransport(state)
    assert strap_pull_job(config, deps, dry_run=True, since_days=3).cli == "wristpull pull strap --since-days 3 --dry-run"
    spec = strap_pull_job(config, deps)
    lines: list[str] = []
    from wristpull.db import connect

    stats = spec.run(connect(config.db_path), lines.append)
    assert stats.files_new == 1 and stats.rows == {"stress": 2} and any("stress: 3 samples" in line for line in lines)


def test_sync_screen_pull_strap_key(config: Config, tmp_path: Path, monkeypatch):
    from datetime import UTC, datetime

    from fake_band import FAKE_ADDRESS, FAKE_KEY, FakeBandState, FakeTransport
    from zepp_buffers import stress_minutes

    config.strap_address = FAKE_ADDRESS
    monkeypatch.setenv("WRISTPULL_STRAP_AUTHKEY", FAKE_KEY.hex())
    state = FakeBandState()
    state.minutes["stress"] = (datetime(2026, 11, 6, 23, 0, tzinfo=UTC), stress_minutes([20, None, 35]))
    deps = make_deps(tmp_path)
    deps.transport_factory = lambda address: FakeTransport(state)

    async def scenario():
        app = make_app(config, deps)
        async with app.run_test() as pilot:
            await pilot.press("2")
            await pilot.press("b")
            await wait_jobs(app)
            record = app.runner.last
            assert record is not None and record.ok and record.spec.cli == "wristpull pull strap"
            assert record.result.files_new == 1 and record.result.rows == {"stress": 2}
            assert any("24400000000001" in line and "stress=3" in line for line in describe_result(record))

    run(scenario())
