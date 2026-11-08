"""Importing from the watch over the MTP tools (FakeMtp serves the garmin_tree fixture)."""

from pathlib import Path

from fake_mtp import FakeMtp
from wristpull.archive import RawArchive
from wristpull.db import table_count
from wristpull.importers.garmin import SOURCE, import_garmin, import_garmin_mtp


def _hashes(conn) -> dict[str, str]:
    return {str(r[0]): str(r[1]) for r in conn.execute("SELECT original_name, sha256 FROM raw_files")}


def test_import_over_mtp_matches_the_directory_import(conn, config, garmin_tree: Path, tmp_path: Path):
    fake = FakeMtp(garmin_tree.parent)
    logs: list[str] = []
    stats = import_garmin(conn, config, mtp_factory=lambda: fake, log=logs.append)
    assert stats.root == "mtp://GARMIN" and stats.device_message == "MTP: fenix 6 Pro (0000deadbeef)"
    assert (stats.found, stats.new, stats.skipped, stats.fetched, stats.errors) == (5, 5, 0, 5, 0)
    assert stats.rows["heart_rate"] == 672 and any("Monitor/M6A10500.FIT" in line for line in logs)
    over_mtp = _hashes(conn)
    rows = conn.execute("SELECT origin_path, device_mtime_utc FROM raw_files WHERE original_name = 'M6A10500.FIT'").fetchone()
    assert rows[0] == "mtp://GARMIN/Monitor/M6A10500.FIT" and rows[1] is None
    assert not list((config.data_dir / "staging").iterdir())  # staging cleaned up
    # the same bytes through the directory path: identical SHA-256, nothing imported twice
    again = import_garmin(conn, config, path=garmin_tree)
    assert (again.new, again.skipped) == (0, 5) and _hashes(conn) == over_mtp
    assert table_count(conn, "heart_rate") == 672


def test_second_run_prefilters_by_name_and_size(conn, config, garmin_tree: Path):
    fake = FakeMtp(garmin_tree.parent)
    import_garmin(conn, config, mtp_factory=lambda: fake)
    fake.fetched.clear()
    logs: list[str] = []
    stats = import_garmin(conn, config, mtp_factory=lambda: fake, log=logs.append)
    assert (stats.found, stats.new, stats.skipped, stats.fetched) == (5, 0, 5, 0) and fake.fetched == []
    assert any("5 already archived" in line for line in logs)
    # a file that grew under the same name (the daily Monitor file) is fetched again
    monitor = garmin_tree / "Monitor" / "M6A10500.FIT"
    monitor.write_bytes(monitor.read_bytes() + b"\x00\x00")
    stats = import_garmin(conn, config, mtp_factory=lambda: fake)
    assert stats.fetched == 1 and stats.found == 5 and stats.new == 1  # the padded file is new (and fails to parse)
    assert RawArchive(config.raw_dir, conn).known_name_sizes(SOURCE) >= {("monitor", "M6A10500.FIT", monitor.stat().st_size)}


def test_dry_run_lists_without_downloading(conn, config, garmin_tree: Path):
    fake = FakeMtp(garmin_tree.parent)
    logs: list[str] = []
    stats = import_garmin(conn, config, mtp_factory=lambda: fake, dry_run=True, log=logs.append)
    assert (stats.found, stats.new, stats.fetched) == (5, 5, 0) and fake.fetched == [] and table_count(conn, "raw_files") == 0
    assert any("would be fetched" in line for line in logs)


def test_stop_between_downloads_and_get_failures(conn, config, garmin_tree: Path):
    fake = FakeMtp(garmin_tree.parent)
    calls = iter([False, False, True])
    stats = import_garmin(conn, config, mtp_factory=lambda: fake, stop=lambda: next(calls, True))
    assert stats.stopped and stats.fetched == 2 and stats.new == 2 and not list((config.data_dir / "staging").iterdir())
    fake = FakeMtp(garmin_tree.parent)
    fake.fail_get.add("/GARMIN/Sleep/S6A10530.FIT")
    stats = import_garmin_mtp(conn, config, mtp=fake)
    assert stats.errors == 1 and stats.error_files[0][0] == "mtp://GARMIN/Sleep/S6A10530.FIT" and (stats.new, stats.skipped, stats.fetched) == (2, 2, 2)


def test_no_device_busy_and_vanishing_watch(conn, config, garmin_tree: Path):
    stats = import_garmin(conn, config, mtp_factory=lambda: FakeMtp(garmin_tree.parent, present=False))
    assert stats.root is None and "no MTP device" in (stats.device_message or "")
    stats = import_garmin(conn, config, mtp_factory=lambda: FakeMtp(garmin_tree.parent, busy=True))
    assert stats.root is None and "holds the watch" in (stats.device_message or "")

    class Vanishing(FakeMtp):
        def get(self, file_id, dest):
            if len(self.fetched) >= 1:
                self.present = False
            super().get(file_id, dest)

    fake = Vanishing(garmin_tree.parent)
    stats = import_garmin_mtp(conn, config, mtp=fake)
    assert stats.fetched == 1 and stats.new == 1 and stats.errors == 1 and "gone" in stats.error_files[0][1] or "no MTP device" in stats.error_files[0][1]


def test_explicit_path_beats_the_mtp_tools(conn, config, garmin_tree: Path):
    stats = import_garmin(conn, config, path=garmin_tree, mtp_factory=lambda: FakeMtp(garmin_tree.parent, present=False))
    assert stats.root == garmin_tree and stats.new == 5
    # without a path the watch is read over MTP, whatever the environment
    stats = import_garmin(conn, config, mtp_factory=lambda: FakeMtp(garmin_tree.parent))
    assert stats.root == "mtp://GARMIN" and stats.skipped == 5
    missing = import_garmin(conn, config, path=garmin_tree / "nope", mtp_factory=lambda: FakeMtp(garmin_tree.parent))
    assert missing.root is None and "is not a directory" in missing.device_message
