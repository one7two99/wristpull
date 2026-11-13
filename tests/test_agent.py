"""The device agent: job kinds, the queue, one runner cycle with the fakes, JSON results, the CLI."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fake_adb import FakeAdb
from fake_band import FAKE_ADDRESS, FAKE_KEY, FakeBandState, FakeTransport
from fake_mtp import FakeMtp
from wristpull.agent import queue
from wristpull.agent.deps import AgentDeps
from wristpull.agent.kinds import KINDS, validate
from wristpull.agent.runner import RESTART_MESSAGE, AgentRunner, status_lines, usb_vendor_present
from wristpull.cli import main
from wristpull.config import Config
from wristpull.db import connect
from wristpull.importers.garmin import ImportStats
from wristpull.importers.phone import PullStats
from wristpull.importers.strap import StrapStats
from wristpull.jobtext import JobRecord, JobSpec, describe_result, jsonable, result_from_json
from wristpull.workouts.push import PushResult, VerifyResult
from wristpull.workouts.watch import RemoveResult, WatchListing, WatchWorkout

NOW = datetime(2026, 11, 13, 7, 0, tzinfo=UTC)
AGENT_DIR = Path(__file__).resolve().parents[1] / "src" / "wristpull" / "agent"


@pytest.fixture
def cfg_file(tmp_path: Path) -> Path:
    """A config file naming the same data directory as the ``config`` fixture (the CLI test reads it)."""
    path = tmp_path / "config.toml"
    path.write_text(f'[storage]\ndata_dir = "{(tmp_path / "Health").as_posix()}"\n[time]\ntimezone = "Europe/Berlin"\n', "utf-8")
    return path


def fake_sysfs(tmp_path: Path, *, watch: bool = False, phone: bool = False) -> Path:
    """A sysfs stand-in: one directory per USB device with idVendor/idProduct, interfaces as <dev>:1.0."""
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir(exist_ok=True)
    if watch:
        dev = sysfs / "1-3"
        dev.mkdir()
        (dev / "idVendor").write_text("091e\n")
        (dev / "idProduct").write_text("4cdb\n")
    if phone:
        dev = sysfs / "1-4"
        dev.mkdir()
        (dev / "idVendor").write_text("18d1\n")
        (dev / "idProduct").write_text("4ee7\n")
        iface = sysfs / "1-4:1.0"
        iface.mkdir()
        (iface / "bInterfaceClass").write_text("ff\n")
        (iface / "bInterfaceSubClass").write_text("42\n")
        (iface / "bInterfaceProtocol").write_text("01\n")
    return sysfs


def deps_for(tmp_path: Path, *, garmin_tree: Path | None = None, sysfs: Path | None = None, transport=None, adb=None) -> AgentDeps:
    mtp_root = garmin_tree.parent if garmin_tree else tmp_path / "no-watch"
    return AgentDeps(
        adb_factory=lambda: adb or FakeAdb(tmp_path / "phone", devices=[]),
        mtp_factory=lambda: FakeMtp(mtp_root, present=garmin_tree is not None),
        transport_factory=transport,
        sleep=lambda _s: None,
        today=lambda: NOW.date(),
        now=lambda: NOW,
        sysfs=sysfs or fake_sysfs(tmp_path),
    )


def runner_for(cfg_file: Path, config: Config, deps: AgentDeps) -> AgentRunner:
    return AgentRunner(cfg_file, deps, load=lambda _p: config)


# ---- kinds ------------------------------------------------------------------------------


def test_validate_types_and_defaults_the_parameters():
    assert validate("import_garmin", {}) == {"dry_run": False}
    assert validate("import_garmin", {"dry_run": "on"}) == {"dry_run": True}
    phone = validate("pull_phone", {"serial": " ABC123 ", "trigger": "0", "wait": "90"})
    assert phone == {"serial": "ABC123", "trigger": False, "sync": True, "wait": 90, "dry_run": False}
    assert validate("pull_strap", {"since_days": ""}) == {"dry_run": False, "since_days": None}
    assert validate("pull_strap", {"since_days": "14"})["since_days"] == 14
    assert validate("workout_push", {"names": "Easy run 05.11..md"})["names"] == ["Easy run 05.11..md"]
    assert validate("watch_remove", {"keys": ["a.fit", " b.fit "]})["keys"] == ["a.fit", "b.fit"]
    for kind, params in (
        ("mount", {}),
        ("import_garmin", {"path": "/etc"}),
        ("pull_phone", {"serial": "x;rm -rf /"}),
        ("pull_phone", {"wait": "abc"}),
        ("pull_phone", {"wait": "5000"}),
        ("pull_strap", {"since_days": "0"}),
        ("workout_push", {"names": []}),
        ("workout_push", {"names": ["../x.md"]}),
        ("watch_remove", {"keys": [".."]}),
    ):
        with pytest.raises(ValueError):
            validate(kind, params)
    assert KINDS["pull_phone"].cli({"serial": "ABC", "trigger": True, "sync": False, "wait": 30, "dry_run": True}) == "wristpull pull phone --serial ABC --no-sync --wait 30 --dry-run"
    assert KINDS["workout_push"].cli({"names": ["a b.md"], "force": True}) == "wristpull workout push 'a b.md' --force"
    assert KINDS["watch_remove"].cli({"keys": ["x.fit"]}) == "wristpull workout remove x.fit --yes"
    assert KINDS["import_garmin"].stoppable and not KINDS["workout_verify"].stoppable


# ---- queue ------------------------------------------------------------------------------


def test_queue_roundtrip_one_job_at_a_time(conn):
    job = queue.enqueue(conn, "import_garmin", {"dry_run": "1"}, now=NOW)
    assert job is not None and job.status == "queued" and job.params == {"dry_run": True} and job.cli == "wristpull import garmin --dry-run"
    assert job.title == "Import Garmin" and job.stoppable and job.seconds(NOW + timedelta(seconds=3)) == 3
    assert queue.enqueue(conn, "pull_strap", {}, now=NOW) is None  # one at a time
    with pytest.raises(ValueError):
        queue.enqueue(conn, "nope", {})
    claimed = queue.claim(conn, now=NOW + timedelta(seconds=1))
    assert claimed is not None and claimed.id == job.id and claimed.running and claimed.started_utc == "2026-11-13T07:00:01Z"
    assert queue.claim(conn) is None  # nothing else queued, one running
    assert queue.current_active(conn).id == job.id and queue.current(conn).id == job.id
    queue.append_log(conn, job.id, "first\n")
    queue.append_log(conn, job.id, "second")
    assert queue.log_lines(conn, job.id) == ["first", "second"] and queue.log_lines(conn, job.id, limit=1) == ["second"]
    assert queue.request_stop(conn, job.id) and queue.stop_requested(conn, job.id)
    queue.finish(conn, job.id, result={"type": "ImportStats", "found": 1}, stopped=True, now=NOW + timedelta(seconds=9))
    done = queue.get(conn, job.id)
    assert done.status == "stopped" and done.result == {"type": "ImportStats", "found": 1} and done.seconds() == 8 and not done.active
    assert not queue.request_stop(conn, job.id)  # no longer active
    assert queue.current_active(conn) is None and queue.current(conn).id == job.id
    second = queue.enqueue(conn, "pull_strap", {"since_days": 3}, now=NOW)
    assert second is not None and [j.id for j in queue.recent(conn)] == [second.id, job.id]
    queue.claim(conn)
    queue.finish(conn, second.id, error="boom", now=NOW)
    assert queue.get(conn, second.id).status == "error"
    for _ in range(3):
        extra = queue.enqueue(conn, "watch_list", {}, now=NOW)
        queue.claim(conn)
        queue.finish(conn, extra.id, result=None, now=NOW)
    assert queue.purge(conn, keep=2) == 3 and len(queue.recent(conn)) == 2
    assert queue.log_lines(conn, job.id) == []  # the log went with the purged job


def test_queue_log_cap_and_stale_jobs(conn):
    job = queue.enqueue(conn, "import_garmin", {}, now=NOW)
    for i in range(6):
        queue.append_log(conn, job.id, f"line {i}", cap=4)
    assert queue.log_lines(conn, job.id) == ["line 2", "line 3", "line 4", "line 5"]
    queue.claim(conn, now=NOW)
    assert queue.fail_stale(conn, "restarted", now=NOW) == [job.id] and queue.get(conn, job.id).error == "restarted"
    assert queue.fail_stale(conn, "restarted") == []


def test_device_state_from_heartbeat(conn):
    assert queue.device_state(conn, now=NOW) == {"online": False, "heartbeat_age_s": None, "last_seen_utc": None, "watch_present": False, "phone_present": False, "version": None}
    queue.set_state(conn, {"heartbeat": "1", "watch_present": "1", "phone_present": "0", "agent_version": "0.2.0"}, now=NOW)
    state = queue.device_state(conn, now=NOW + timedelta(seconds=5))
    assert state["online"] and state["watch_present"] and not state["phone_present"] and state["heartbeat_age_s"] == 5 and state["version"] == "0.2.0"
    stale = queue.device_state(conn, now=NOW + timedelta(seconds=60))
    assert not stale["online"] and not stale["watch_present"] and stale["last_seen_utc"] == "2026-11-13T07:00:00Z"


# ---- runner -----------------------------------------------------------------------------


def test_run_once_heartbeat_and_usb_presence(cfg_file: Path, config: Config, tmp_path: Path):
    sysfs = fake_sysfs(tmp_path, watch=True, phone=True)
    assert usb_vendor_present(sysfs) and not usb_vendor_present(sysfs, "18d2") and not usb_vendor_present(tmp_path / "missing")
    runner = runner_for(cfg_file, config, deps_for(tmp_path, sysfs=sysfs))
    assert runner.run_once() is None  # nothing queued
    conn = connect(config.db_path)
    state = queue.device_state(conn, now=NOW)
    assert state["online"] and state["watch_present"] and state["phone_present"]


def test_run_once_imports_from_the_fake_watch(cfg_file: Path, config: Config, tmp_path: Path, garmin_tree: Path):
    conn = connect(config.db_path)
    job = queue.enqueue(conn, "import_garmin", {}, now=NOW)
    runner = runner_for(cfg_file, config, deps_for(tmp_path, garmin_tree=garmin_tree))
    done = runner.run_once()
    assert done is not None and done.id == job.id and done.status == "done" and done.error is None
    assert done.result["type"] == "ImportStats" and done.result["new"] == 5 and done.result["root"] == "mtp://GARMIN"
    log = queue.log_lines(conn, job.id)
    assert log[0].startswith("▶ Import Garmin   $ wristpull import garmin") and any("fetched" in line or "new" in line for line in log)
    result = result_from_json(done.result)
    assert isinstance(result, ImportStats) and result.new == 5 and result.rows["heart_rate"] > 0
    lines = describe_result(JobRecord(JobSpec("import_garmin", "Import Garmin", done.cli, None), result=result))
    assert lines[0] == "✓ 5 FIT files, 5 new, 0 already archived, 0 errors" and lines[1].startswith("  inserted: ")
    # the second run finds everything archived; the agent reloads the config for each job
    queue.enqueue(conn, "import_garmin", {"dry_run": True}, now=NOW)
    again = runner.run_once()
    assert again.status == "done" and again.result["skipped"] == 5 and again.result["new"] == 0
    assert runner.run_once() is None


def test_run_once_without_a_watch_and_with_a_failing_phone(cfg_file: Path, config: Config, tmp_path: Path):
    conn = connect(config.db_path)
    queue.enqueue(conn, "import_garmin", {}, now=NOW)
    runner = runner_for(cfg_file, config, deps_for(tmp_path))
    done = runner.run_once()
    assert done.status == "done" and done.result["root"] is None and "no MTP device" in done.result["device_message"]
    record = JobRecord(JobSpec("import_garmin", "Import Garmin", "", None), result=result_from_json(done.result))
    assert describe_result(record)[0].startswith("✗ No Garmin device found")
    queue.enqueue(conn, "pull_phone", {"wait": 0}, now=NOW)
    failed = runner.run_once()
    assert failed.status == "error" and "no" in failed.error.lower()
    assert queue.log_lines(conn, failed.id)[-1].startswith("✗ ")


def test_run_once_stops_between_files_and_before_start(cfg_file: Path, config: Config, tmp_path: Path, garmin_tree: Path):
    conn = connect(config.db_path)
    job = queue.enqueue(conn, "import_garmin", {}, now=NOW)
    queue.request_stop(conn, job.id)
    runner = runner_for(cfg_file, config, deps_for(tmp_path, garmin_tree=garmin_tree))
    done = runner.run_once()
    assert done.status == "stopped" and done.result is None and queue.log_lines(conn, job.id)[-1] == "⏹ stopped before it started"
    # stop requested while the import runs: the library polls the flag between files and keeps what it has
    job = queue.enqueue(conn, "import_garmin", {}, now=NOW)
    original = queue.stop_requested

    def stop_after_first_poll(c, job_id, _calls=[]):  # noqa: B006 - a counter
        _calls.append(1)
        if len(_calls) == 3:  # the runner's own check, then the poll before the first file, then this one
            queue.request_stop(c, job_id)
        return original(c, job_id)

    import wristpull.agent.runner as runner_module

    runner_module.queue.stop_requested = stop_after_first_poll
    try:
        done = runner.run_once()
    finally:
        runner_module.queue.stop_requested = original
    assert done.status == "stopped" and done.result["stopped"] is True and 0 < done.result["new"] < 5


def test_run_once_pulls_the_fake_strap(cfg_file: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WRISTPULL_STRAP_AUTHKEY", "0x" + FAKE_KEY.hex())
    config = Config(data_dir=tmp_path / "data", timezone="Europe/Berlin", strap_address=FAKE_ADDRESS, strap_since_days=3)
    conn = connect(config.db_path)
    state = FakeBandState()
    queue.enqueue(conn, "pull_strap", {"since_days": 2}, now=NOW)
    runner = runner_for(cfg_file, config, deps_for(tmp_path, transport=lambda _address: FakeTransport(state)))
    done = runner.run_once()
    assert done.status == "done" and done.result["type"] == "StrapStats" and done.result["address"] == FAKE_ADDRESS
    assert isinstance(result_from_json(done.result), StrapStats)


def test_start_marks_stale_jobs_and_serve_runs_until_stopped(cfg_file: Path, config: Config, tmp_path: Path):
    conn = connect(config.db_path)
    job = queue.enqueue(conn, "watch_list", {}, now=NOW)
    queue.claim(conn, now=NOW)
    runner = runner_for(cfg_file, config, deps_for(tmp_path))
    assert runner.start() == [job.id] and queue.get(conn, job.id).error == RESTART_MESSAGE
    assert queue.get_state(conn)["agent_version"][0]

    def sleep(_s: float) -> None:
        if runner.cycles >= 3:
            runner.stop_serving = True

    runner.deps.sleep = sleep
    assert runner.serve(0.0, install_signals=False) == 0 and runner.cycles == 3
    lines = status_lines(conn, now=NOW)
    assert lines[0].startswith("agent:      online") and lines[1] == "watch:      not attached" and lines[3].startswith(f"job:        #{job.id} watch_list error")


def test_runner_has_no_sql():
    forbidden = re.compile(r"\bSELECT\b|\.execute\(")
    offenders = [p.name for p in AGENT_DIR.glob("*.py") if p.name != "queue.py" and forbidden.search(p.read_text("utf-8"))]
    assert not offenders, offenders


# ---- JSON results -----------------------------------------------------------------------


def test_results_survive_the_json_roundtrip():
    stats = PullStats(device="X", db_pulled=True, db_new=True, db_devices=["band"], fit_found=2, fit_new=1, rows={"steps": 3}, messages=["m"])
    data = jsonable(stats)
    assert data["type"] == "PullStats" and result_from_json(data) == stats
    push = PushResult(root=Path("/GARMIN"), target=Path("/GARMIN/NewFiles"), written=[Path("/GARMIN/NewFiles/a.fit")], invalid=[("b.md", ["bad"])], errors=[("c.fit", "exists")], device="mtp")
    back = result_from_json(jsonable(push))
    assert back == push and isinstance(back.written[0], Path)
    verify = VerifyResult(root=Path("/GARMIN"), on_watch=["a"], verified=["a"], missing=[])
    assert result_from_json(jsonable(verify)) == verify
    item = WatchWorkout(path=Path("/GARMIN/Workouts/a.fit"), name="a", sport="running", steps=3, time_created_utc="2026-11-01T00:00:00Z", size=10, mtime_utc=None, sha256="x", origin="wristpull", data=b"bytes", locator="mtp:/GARMIN/Workouts/a.fit")
    listing = WatchListing(root=Path("/GARMIN"), items=[item], device="mtp://GARMIN", watch=object())
    data = jsonable(listing)
    assert "watch" not in data and data["items"][0]["data"] is None and "workout" not in data["items"][0]
    back = result_from_json(data)
    assert isinstance(back, WatchListing) and back.watch is None and back.items[0].data == b"" and back.items[0].name == "a" and back.active == back.items
    removed = RemoveResult(removed=["a.fit"], backups=[Path("/data/workouts/watch-backup/a.fit")], listing=listing)
    back = result_from_json(jsonable(removed))
    assert back.removed == ["a.fit"] and isinstance(back.listing, WatchListing) and back.backups == removed.backups
    assert result_from_json({"free": [1, {"type": "Unknown", "x": 1}]}) == {"free": [1, {"type": "Unknown", "x": 1}]}
    assert jsonable({"path": Path("/x"), "set": {1}, "obj": object()})["path"] == "/x"


# ---- CLI --------------------------------------------------------------------------------


def test_agent_cli_once_and_status(cfg_file: Path, capsys, monkeypatch):
    import wristpull.agent.runner as runner_module

    monkeypatch.setattr(runner_module, "SYSFS_USB", Path("/nonexistent"), raising=False)
    assert main(["--config", str(cfg_file), "agent", "--once"]) == 0
    out = capsys.readouterr().out
    assert "heartbeat written, no queued job" in out
    assert main(["--config", str(cfg_file), "agent", "--status"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("agent:      online") and "job:        none yet" in out
