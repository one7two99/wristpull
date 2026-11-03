import errno
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from wristpull.config import Config
from wristpull.fit.decode import decode_fit
from wristpull.fit.workout import build_workout_fit, sport_name, workout_from_fit
from wristpull.fit.writer import FitWriter, ts
from wristpull.workouts.model import HeartRate, Step, Workout
from wristpull.workouts.push import push
from wristpull.workouts.store import push_status, pushes, save, upcoming
from wristpull.workouts.watch import (
    WORKOUT_LIMIT_WARN,
    backup_dir,
    backup_watch_workout,
    list_watch_workouts,
    read_watch,
    remove_device_file,
    remove_from_watch,
    remove_watch_workout,
)

NOW = datetime(2026, 11, 2, 20, 0, tzinfo=UTC)


def _w(day: date, name: str = "Easy run", sport: str = "running") -> Workout:
    return Workout(
        name=name,
        day=day,
        sport=sport,
        steps=[
            Step("warmup", duration_s=600, hr=HeartRate("bpm", high=130)),
            Step("active", distance_m=5000, hr=HeartRate("bpm", low=135, high=150)),
            Step("recovery", duration_s=120, hr=HeartRate("percent", low=60, high=70)),
            Step("active", duration_s=300, hr=HeartRate("zone", zone=4)),
            Step("cooldown", duration_s=300),
        ],
    )


def foreign_fit() -> bytes:
    """A Connect-like strength workout with rep steps and a pace target (things wristpull cannot express)."""
    w = FitWriter(big_endian=True)
    w.message(0, [(0, "enum", 5), (1, "uint16", 1), (2, "uint16", 65534), (3, "uint32z", 175), (4, "uint32", ts(datetime(2019, 6, 20, 3, 37, 40, tzinfo=UTC)))])
    w.message(26, [(254, "uint16", 0), (4, "enum", 10), (11, "enum", 20), (6, "uint16", 3), (8, "string", "30-Minute Blast")])
    w.message(27, [(254, "uint16", 0), (7, "enum", 0), (1, "enum", 29), (2, "uint32", 8), (3, "enum", 2)])
    w.message(27, [(254, "uint16", 1), (7, "enum", 0), (1, "enum", 1), (2, "uint32", 100000), (3, "enum", 0), (5, "uint32", 3401), (6, "uint32", 3571)])
    w.message(27, [(254, "uint16", 2), (7, "enum", 1), (1, "enum", 6), (2, "uint32", 0), (3, "enum", 2), (4, "uint32", 3)])
    return w.finish()


def test_workout_from_fit_roundtrip():
    original = _w(date(2026, 11, 5))
    back = workout_from_fit(decode_fit(build_workout_fit(original, created=NOW)))
    assert back.name == "Easy run" and back.sport == "running"
    assert back.steps == original.steps
    strength = workout_from_fit(decode_fit(build_workout_fit(_w(date(2026, 11, 5), "Core", "strength"), created=NOW)))
    assert strength.sport == "strength"


def test_workout_from_fit_foreign_file():
    w = workout_from_fit(decode_fit(foreign_fit()))
    assert w.name == "30-Minute Blast" and w.sport == "strength"
    assert w.steps[0].kind == "open" and w.steps[0].note == "reps 8"
    assert w.steps[1].distance_m == 1000 and w.steps[1].hr is None and w.steps[1].note == "pace target"
    assert w.steps[2].intensity == "repeat" and w.steps[2].repeat == 3 and w.steps[2].repeat_from == 0
    assert sport_name(1, 0) == "running" and sport_name(10, 99) == "training" and sport_name(2, 6) == "cycling" and sport_name(77, 0) == "sport 77"


def _watch(garmin_tree: Path) -> Path:
    folder = garmin_tree / "Workouts"
    folder.mkdir()
    (folder / "Schedule").mkdir()
    (folder / "Schedule" / "2026-08-24.fit").write_bytes(build_workout_fit(_w(date(2026, 8, 24), "Scheduled"), created=NOW))
    (folder / "Guided").mkdir()
    (folder / "30-Minute_Blast_workout.fit").write_bytes(foreign_fit())
    (folder / "broken_workout.fit").write_bytes(b"not a fit file")
    return folder


def test_list_watch_workouts_top_level_only_with_origin(config: Config, conn, garmin_tree: Path):
    folder = _watch(garmin_tree)
    save(config, _w(date(2026, 11, 5)))
    from wristpull.workouts.store import list_workouts

    result = push(conn, config, list_workouts(config), roots=lambda: [garmin_tree.parent], mounter=lambda: (False, "no"), folder="Workouts")
    items = list_watch_workouts(garmin_tree, conn)
    assert [i.name for i in items] == ["30-Minute Blast", "broken_workout", "Easy run"]  # sub-folders ignored
    blast, broken, easy = items
    assert blast.origin == "other" and blast.sport == "strength" and blast.steps == 3 and blast.time_created_utc == "2019-06-20T03:37:40Z"
    assert broken.error and broken.workout is None
    assert easy.origin == "wristpull" and easy.path == result.written[0] and easy.size == len(easy.path.read_bytes())
    assert list_watch_workouts(garmin_tree)[2].origin == "other"  # without a connection nothing is known
    assert (folder / "Schedule" / "2026-08-24.fit").exists()
    listing = read_watch(conn, config, roots=lambda: [garmin_tree.parent], mounter=lambda: (False, "no"))
    assert listing.root == garmin_tree and len(listing.items) == 3 and not listing.near_limit
    assert read_watch(conn, config, roots=lambda: [], mounter=lambda: (False, "gone")).mount_message == "gone"


def test_remove_backs_up_marks_history_and_refuses_other_folders(config: Config, conn, garmin_tree: Path):
    folder = _watch(garmin_tree)
    save(config, _w(date(2026, 11, 5)))
    from wristpull.workouts.store import list_workouts

    md = list_workouts(config)[0]
    push(conn, config, [md], roots=lambda: [garmin_tree.parent], mounter=lambda: (False, "no"), folder="Workouts")
    assert push_status(conn, md.path) == "pushed"
    easy = next(i for i in list_watch_workouts(garmin_tree, conn) if i.name == "Easy run")
    data = easy.path.read_bytes()
    backup = remove_watch_workout(conn, config, easy)
    assert not easy.path.exists() and backup.parent == backup_dir(config) and backup.read_bytes() == data
    assert push_status(conn, md.path) == "removed" and pushes(conn)[0]["status"] == "removed"
    # removed counts as "not on the watch": upcoming picks it again without force
    assert [w.name for w in upcoming(config, conn, date(2026, 11, 1))] == ["Easy run"]
    # an identical second backup reuses the first file
    easy.path.write_bytes(data)
    assert backup_watch_workout(config, easy) == backup and len(list(backup_dir(config).iterdir())) == 1
    # anything outside the top level of Workouts is refused
    from wristpull.workouts.watch import read_watch_workout

    nested = read_watch_workout(folder / "Schedule" / "2026-08-24.fit")
    with pytest.raises(ValueError, match="only workout files directly"):
        remove_watch_workout(conn, config, nested)
    assert (folder / "Schedule" / "2026-08-24.fit").exists()


def test_remove_from_watch_by_name_and_file(config: Config, conn, garmin_tree: Path):
    _watch(garmin_tree)
    log: list[str] = []
    roots = lambda: [garmin_tree.parent]  # noqa: E731
    result = remove_from_watch(conn, config, ["30-minute blast", "broken_workout.fit", "nope"], roots=roots, mounter=lambda: (False, "no"), log=log.append)
    assert sorted(result.removed) == ["active/30-Minute_Blast_workout.fit", "active/broken_workout.fit"] and len(result.backups) == 2
    assert result.errors == [("nope", "not found under GARMIN/Workouts or NewFiles")]
    assert result.listing is not None and result.listing.items == []
    assert any("not found" in line for line in log)
    gone = remove_from_watch(conn, config, ["x"], roots=lambda: [], mounter=lambda: (False, "no"))
    assert gone.listing.root is None and gone.removed == []


class _NoUnlink(type(Path())):
    def unlink(self, missing_ok=False):  # noqa: D401
        raise OSError(errno.ENOTSUP, "Operation not supported")


def test_remove_device_file_falls_back_to_gio_remove(tmp_path: Path):
    calls: list[list[str]] = []
    target = _NoUnlink(tmp_path / "w.fit")
    target.write_bytes(b"x")
    assert remove_device_file(target, runner=lambda argv: (calls.append(argv), (0, "", ""))[1]) == "gio"
    assert calls == [["gio", "remove", str(target)]]
    with pytest.raises(OSError, match="gio remove"):
        remove_device_file(target, runner=lambda argv: (1, "", "busy"))
    plain = tmp_path / "p.fit"
    plain.write_bytes(b"x")
    assert remove_device_file(plain) == "unlink" and not plain.exists()
    with pytest.raises(FileNotFoundError):
        remove_device_file(plain)


def test_force_push_and_upcoming(config: Config, conn, garmin_tree: Path):
    save(config, _w(date(2026, 11, 5)))
    save(config, _w(date(2026, 10, 1), "Past"))
    roots = lambda: [garmin_tree.parent]  # noqa: E731
    today = date(2026, 11, 2)
    first = upcoming(config, conn, today)
    assert [w.name for w in first] == ["Easy run"]  # the past one is not upcoming
    push(conn, config, first, roots=roots, mounter=lambda: (False, "no"))
    assert upcoming(config, conn, today) == [] and [w.name for w in upcoming(config, conn, today, force=True)] == ["Easy run"]
    # force writes again even when the same content is in the folder
    from wristpull.workouts.store import list_workouts

    md = [w for w in list_workouts(config) if w.name == "Easy run"]
    log: list[str] = []
    before = set((garmin_tree / "NewFiles").iterdir())
    again = push(conn, config, md, roots=roots, mounter=lambda: (False, "no"), force=True, log=log.append)
    after = set((garmin_tree / "NewFiles").iterdir())
    assert len(after - before) == 1 and again.written[0] in after - before
    assert len([n for n in after if n.name.startswith("2026-11-05-easy-run")]) == 2
    assert WORKOUT_LIMIT_WARN == 20


def test_pending_files_in_newfiles(config: Config, conn, garmin_tree: Path):
    """Pushed files wait in NewFiles until unplugging: listed as pending, counted, removable, never 'verified'."""
    from wristpull.fit.workout import build_schedule_fit
    from wristpull.workouts.push import verify
    from wristpull.workouts.store import list_workouts

    _watch(garmin_tree)
    roots = lambda: [garmin_tree.parent]  # noqa: E731
    save(config, _w(date(2026, 11, 5)))
    save(config, _w(date(2026, 11, 6), "30-Minute Blast"))  # same name as the active foreign workout
    push(conn, config, list_workouts(config), roots=roots, mounter=lambda: (False, "no"))
    newfiles = garmin_tree / "NewFiles"
    (newfiles / "schedule-x.fit").write_bytes(build_schedule_fit(list_workouts(config)))
    (newfiles / "note.txt").write_text("not fit", "utf-8")
    (newfiles / "garbage.fit").write_bytes(b"nope")
    listing = read_watch(conn, config, roots=roots, mounter=lambda: (False, "no"))
    assert [(i.location, i.name) for i in listing.items] == [("active", "30-Minute Blast"), ("active", "broken_workout"), ("pending", "30-Minute Blast"), ("pending", "Easy run")]
    assert len(listing.active) == 2 and len(listing.pending) == 2
    pending_blast = listing.pending[0]
    assert pending_blast.name_clash and not listing.pending[1].name_clash and not listing.active[0].name_clash
    assert pending_blast.origin == "wristpull" and pending_blast.key == f"pending/{pending_blast.path.name}"
    # verify only confirms what the watch took over (active), not what still waits in NewFiles
    check = verify(conn, config, roots=roots, mounter=lambda: (False, "no"))
    assert check.verified == [] and sorted(check.missing) == ["30-Minute Blast", "Easy run"]
    # a push can be taken back before unplugging
    easy = listing.pending[1]
    result = remove_from_watch(conn, config, [easy.key], roots=roots, mounter=lambda: (False, "no"))
    assert result.removed == [easy.key] and not easy.path.exists() and len(result.listing.pending) == 1
    md = next(w for w in list_workouts(config) if w.name == "Easy run")
    assert push_status(conn, md.path) == "removed"
    # the near-limit check counts pending files as well
    for i in range(WORKOUT_LIMIT_WARN):
        (newfiles / f"extra{i}.fit").write_bytes(build_workout_fit(_w(date(2026, 12, 1), f"Extra {i}"), created=NOW))
    assert read_watch(conn, config, roots=roots, mounter=lambda: (False, "no")).near_limit


def test_workout_from_fit_reads_repeat_blocks():
    from datetime import date as _date

    from wristpull.workouts.model import Defaults, template

    original = template("intervals", _date(2026, 11, 7), Defaults())
    back = workout_from_fit(decode_fit(build_workout_fit(original, created=NOW)))
    assert back.steps[3].intensity == "repeat" and back.steps[3].repeat == 6 and back.steps[3].repeat_from == 1
    assert back.total_metres == original.total_metres
