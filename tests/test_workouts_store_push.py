from datetime import date
from pathlib import Path

import pytest

from wristpull.config import Config
from wristpull.fit.decode import decode_fit
from wristpull.workouts.model import Step, Workout, parse_markdown
from wristpull.workouts.push import device_folder, push, verify, workouts_on_watch
from wristpull.workouts.store import existing_for_day, list_workouts, load, output_dir, pending_pushes, push_status, pushes, save, set_status, workouts_dir


def _w(day: date, name: str = "Easy run") -> Workout:
    return Workout(name=name, day=day, steps=[Step("warmup", duration_s=600), Step("active", duration_s=1800)])


def test_save_list_and_status(config: Config):
    assert list_workouts(config) == []
    path = save(config, _w(date(2026, 11, 5)))
    assert path == workouts_dir(config) / "2026-11-05-easy-run.md" and path.exists()
    with pytest.raises(FileExistsError):
        save(config, _w(date(2026, 11, 5)))
    save(config, _w(date(2026, 11, 3), "Tempo"))
    (workouts_dir(config) / "broken.md").write_text("- run ten\n", "utf-8")
    items = list_workouts(config)
    assert [w.name for w in items] == ["broken", "Tempo", "Easy run"]  # undated first, then by date
    assert not items[0].valid and items[1].valid
    assert [w.name for w in list_workouts(config, start=date(2026, 11, 4))] == ["broken", "Easy run"]
    assert [w.name for w in list_workouts(config, start=date(2026, 11, 1), end=date(2026, 11, 4))] == ["broken", "Tempo"]
    assert existing_for_day(config, date(2026, 11, 5)) == [path] and existing_for_day(config, date(2026, 11, 9)) == []
    # set_status rewrites only the status line
    text = path.read_text("utf-8")
    set_status(path, "pushed")
    assert path.read_text("utf-8") == text.replace("status: draft", "status: pushed")
    plain = workouts_dir(config) / "plain.md"
    plain.write_text("# Plain\n- run 10 min\n", "utf-8")
    set_status(plain, "ready")
    assert plain.read_text("utf-8").startswith("---\nstatus: ready\n---\n# Plain")
    assert load(workouts_dir(config) / "missing.md").errors[0].startswith("cannot read")


def test_push_dry_run_and_to_device(config: Config, conn, garmin_tree: Path):
    save(config, _w(date(2026, 11, 5)))
    save(config, _w(date(2026, 11, 6), "Tempo"))
    (workouts_dir(config) / "bad.md").write_text("---\ndate: 2026-11-07\nname: Bad\n---\n- sprint 10 min\n", "utf-8")
    items = list_workouts(config)
    easy, tempo, bad = items  # sorted by date: 11-05, 11-06, 11-07
    assert not bad.valid
    log: list[str] = []
    result = push(conn, config, items, dry_run=True, log=log.append)
    assert result.dry_run and result.target == output_dir(config) and len(result.written) == 2 and result.schedule is None
    assert result.invalid[0][0] == "Bad" and result.invalid[0][1][0].startswith("unknown intensity 'sprint'") and not result.ok
    assert all(p.exists() and p.name.startswith("2026-11-0") for p in result.written)
    assert push_status(conn, tempo.path) is None  # dry runs do not change the status
    assert load(Path(tempo.path)).status == "draft"
    # real push to a fake watch: files land in NewFiles, status flips to pushed, history recorded
    result = push(conn, config, [easy, tempo], roots=lambda: [garmin_tree.parent], mounter=lambda: (False, "no"), schedule=True, log=log.append)
    assert result.ok and result.root == garmin_tree and result.target == garmin_tree / "NewFiles"
    assert len(result.written) == 2 and result.schedule is not None and result.schedule.exists()
    assert decode_fit(result.written[0].read_bytes()).file_type == "workout" and decode_fit(result.schedule.read_bytes()).file_type == "schedules"
    assert load(Path(tempo.path)).status == "pushed" and push_status(conn, tempo.path) == "pushed"
    history = pushes(conn)
    assert len(history) == 4 and history[0]["status"] == "pushed" and history[0]["watch_serial"] == "3999999999" and history[-1]["status"] == "dry_run"
    # pushing again writes nothing new on the device (same content hash) but records the push
    before = sorted(p.name for p in (garmin_tree / "NewFiles").iterdir())
    push(conn, config, [tempo], roots=lambda: [garmin_tree.parent], mounter=lambda: (False, "no"), log=log.append)
    assert sorted(p.name for p in (garmin_tree / "NewFiles").iterdir()) == before and any("already on the device" in line for line in log)
    # Workouts folder as the target, matching the existing folder's case
    (garmin_tree / "WORKOUTS").mkdir()
    result = push(conn, config, [easy], roots=lambda: [garmin_tree.parent], mounter=lambda: (False, "no"), folder="Workouts")
    assert result.target == garmin_tree / "WORKOUTS" and device_folder(garmin_tree, "newfiles") == garmin_tree / "NewFiles"


def test_push_without_device(config: Config, conn):
    save(config, _w(date(2026, 11, 5)))
    result = push(conn, config, list_workouts(config), roots=lambda: [], mounter=lambda: (False, "nothing attached"))
    assert result.root is None and result.mount_message == "nothing attached" and result.written == []
    assert pending_pushes(conn) == []
    assert push(conn, config, [parse_markdown("# x\n")], roots=lambda: []).root is None


def test_verify_marks_workouts_the_watch_took_over(config: Config, conn, garmin_tree: Path):
    save(config, _w(date(2026, 11, 5)))
    save(config, _w(date(2026, 11, 6), "Tempo"))
    items = list_workouts(config)
    roots = lambda: [garmin_tree.parent]  # noqa: E731
    result = push(conn, config, items, roots=roots, mounter=lambda: (False, "no"))
    assert len(pending_pushes(conn)) == 2
    # nothing under Workouts yet
    check = verify(conn, config, roots=roots, mounter=lambda: (False, "no"))
    assert check.root == garmin_tree and check.on_watch == [] and check.missing == ["Easy run", "Tempo"] and check.verified == []
    # the watch "processes" the first file: it appears under Workouts (and some foreign file too)
    (garmin_tree / "Workouts").mkdir()
    (garmin_tree / "Workouts" / "W1.FIT").write_bytes(result.written[0].read_bytes())
    (garmin_tree / "Workouts" / "junk.fit").write_bytes(b"not fit")
    assert workouts_on_watch(garmin_tree) == [("Easy run", "2026-11-05T12:00:00Z")]
    log: list[str] = []
    check = verify(conn, config, roots=roots, mounter=lambda: (False, "no"), log=log.append)
    assert check.verified == ["Easy run"] and check.missing == ["Tempo"]
    assert load(Path(items[0].path)).status == "verified" and push_status(conn, items[0].path) == "verified"
    assert [r["name"] for r in pending_pushes(conn)] == ["Tempo"]
    assert any("✓ Easy run" in line for line in log)
    assert verify(conn, config, roots=lambda: [], mounter=lambda: (False, "gone")).root is None
