"""Workouts to and from the watch over the MTP tools (FakeMtp serves the garmin_tree fixture)."""

from datetime import date
from pathlib import Path

from fake_mtp import FakeMtp
from wristpull.config import Config
from wristpull.workouts.device import MtpWatch, open_watch, watch_present
from wristpull.workouts.model import Step, Workout
from wristpull.workouts.push import push, verify
from wristpull.workouts.store import list_workouts, push_status, pushes, save
from wristpull.workouts.watch import backup_dir, read_watch, remove_from_watch


def _w(day: date, name: str = "Easy run") -> Workout:
    return Workout(name=name, day=day, steps=[Step("warmup", duration_s=600), Step("active", duration_s=1800)])


def test_push_over_mtp_records_locator_and_unit_id(config: Config, conn, garmin_tree: Path):
    (garmin_tree / "NewFiles").mkdir()
    save(config, _w(date(2026, 11, 5)))
    fake = FakeMtp(garmin_tree.parent)
    log: list[str] = []
    result = push(conn, config, list_workouts(config), access="mtp", mtp_factory=lambda: fake, log=log.append)
    assert result.ok and result.root == Path("/GARMIN") and result.target == Path("/GARMIN/NewFiles")
    assert result.device == "mtp://GARMIN on fenix 6 Pro (0000deadbeef)" and result.mount_message == "MTP: fenix 6 Pro (0000deadbeef)"
    assert len(result.written) == 1 and result.written[0].name.startswith("2026-11-05-easy-run-")
    assert fake.sent == [("/GARMIN/NewFiles", result.written[0].name)] and (garmin_tree / "NewFiles" / result.written[0].name).exists()
    row = pushes(conn)[0]
    assert row["target"] == f"mtp://GARMIN/NewFiles/{result.written[0].name}" and row["watch_serial"] == "3999999999"
    assert any("over MTP" in line for line in log)
    # a second push does not send again; force writes under a stamped name
    fake.sent.clear()
    again = push(conn, config, list_workouts(config), access="mtp", mtp_factory=lambda: fake, log=log.append)
    assert again.ok and fake.sent == [] and any("already on the device" in line for line in log)
    forced = push(conn, config, list_workouts(config), access="mtp", mtp_factory=lambda: fake, force=True)
    assert len(fake.sent) == 1 and forced.written[0].name != result.written[0].name


def test_push_to_a_missing_folder_is_reported_not_raised(config: Config, conn, garmin_tree: Path):
    save(config, _w(date(2026, 11, 5)))
    fake = FakeMtp(garmin_tree.parent)  # no Workouts folder in the tree
    result = push(conn, config, list_workouts(config), access="mtp", mtp_factory=lambda: fake, folder="Workouts", schedule=True)
    assert not result.ok and result.errors and "no folder /GARMIN/Workouts" in result.errors[0][1] and result.written == [] and result.schedule is None
    assert pushes(conn) == []  # nothing recorded for a failed write
    absent = push(conn, config, list_workouts(config), access="mtp", mtp_factory=lambda: FakeMtp(garmin_tree.parent, present=False))
    assert absent.root is None and "no MTP device" in (absent.mount_message or "")


def test_verify_and_read_watch_over_mtp(config: Config, conn, garmin_tree: Path):
    (garmin_tree / "NewFiles").mkdir()
    (garmin_tree / "Workouts").mkdir()
    md = save(config, _w(date(2026, 11, 5)))
    fake = FakeMtp(garmin_tree.parent)
    result = push(conn, config, list_workouts(config), access="mtp", mtp_factory=lambda: fake)
    check = verify(conn, config, access="mtp", mtp_factory=lambda: fake)
    assert check.root == Path("/GARMIN") and check.missing == ["Easy run"] and check.verified == []
    # the watch "imports" the file at unplug: it moves to Workouts
    name = result.written[0].name
    (garmin_tree / "NewFiles" / name).rename(garmin_tree / "Workouts" / "W1.FIT")
    check = verify(conn, config, access="mtp", mtp_factory=lambda: fake)
    assert check.verified == ["Easy run"] and push_status(conn, str(md)) == "verified"
    listing = read_watch(conn, config, access="mtp", mtp_factory=lambda: fake)
    assert listing.device and listing.root == Path("/GARMIN") and [i.name for i in listing.active] == ["Easy run"]
    item = listing.active[0]
    assert item.path == Path("/GARMIN/Workouts/W1.FIT") and item.locator == "mtp://GARMIN/Workouts/W1.FIT" and item.mtime_utc is None
    assert item.origin == "wristpull" and item.data and item.key == "active/W1.FIT"


def test_remove_over_mtp_backs_up_from_the_listing_and_relists_cheaply(config: Config, conn, garmin_tree: Path):
    (garmin_tree / "Workouts").mkdir()
    save(config, _w(date(2026, 11, 5)))
    save(config, _w(date(2026, 11, 6), "Tempo"))
    fake = FakeMtp(garmin_tree.parent)
    push(conn, config, list_workouts(config), access="mtp", mtp_factory=lambda: fake, folder="Workouts")
    fake.fetched.clear()
    log: list[str] = []
    result = remove_from_watch(conn, config, ["Easy run", "nope"], access="mtp", mtp_factory=lambda: fake, log=log.append)
    assert result.removed and result.removed[0].startswith("active/2026-11-05-easy-run-") and len(fake.deleted) == 1
    assert result.backups[0].parent == backup_dir(config) and result.backups[0].exists()
    assert result.errors == [("nope", "not found under GARMIN/Workouts or NewFiles")]
    assert [i.name for i in result.listing.items] == ["Tempo"]
    # the listing fetched each file once (GarminDevice.xml not needed here); the re-read reused the parsed Tempo
    fetched_names = [path.rsplit("/", 1)[-1] for _id, path in fake.fetched]
    assert fetched_names.count("2026-11-06-tempo-" + [n for n in fetched_names if n.startswith("2026-11-06-tempo-")][0][len("2026-11-06-tempo-"):]) == 1
    assert pushes(conn)[-1]["status"] == "removed" or any(r["status"] == "removed" for r in pushes(conn))


def test_open_watch_dispatch_and_presence(config: Config, garmin_tree: Path, monkeypatch):
    fake = FakeMtp(garmin_tree.parent)
    watch, message = open_watch(config, access="mtp", mtp_factory=lambda: fake)
    assert isinstance(watch, MtpWatch) and message == "MTP: fenix 6 Pro (0000deadbeef)"
    assert watch.unit_id() == "3999999999" and watch.unit_id() == "3999999999" and len([c for c in fake.calls if c.startswith("get")]) == 1
    assert watch.folder_path("monitor") == Path("/GARMIN/Monitor") and watch.folder_path("Nope") == Path("/GARMIN/Nope")
    assert watch.eject() == (False, "MTP: nothing to eject, the tools close the device after every call")
    assert [e.name for e in watch.list("Monitor")] == ["M6A10500.FIT"] and watch.list("Nope") == []
    assert watch.exists("Monitor", "m6a10500.fit") and not watch.exists("Monitor", "x.fit")
    # an explicit path is always a directory, whatever the access
    local, _msg = open_watch(config, garmin_tree, access="mtp", mtp_factory=lambda: FakeMtp(garmin_tree.parent, present=False))
    assert local is not None and local.root == garmin_tree
    none, why = open_watch(config, access="mtp", mtp_factory=lambda: FakeMtp(garmin_tree.parent, busy=True))
    assert none is None and "holds the watch" in why
    assert watch_present(config, roots=lambda: [garmin_tree.parent], access="gvfs") is True
    assert watch_present(config, roots=lambda: [], access="gvfs") is False
    assert watch_present(config, access="mtp") is None
    monkeypatch.setenv("WRISTPULL_IN_CONTAINER", "1")
    assert watch_present(config) is None  # auto resolves to mtp inside the container
