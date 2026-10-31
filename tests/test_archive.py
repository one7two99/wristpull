from datetime import UTC, datetime

import pytest

from wristpull.archive import RawArchive, safe_name


def test_safe_name():
    assert safe_name("2026-10-06-08-00-00.fit") == "2026-10-06-08-00-00.fit"
    assert safe_name("../evil name?.FIT") == "evil_name_.FIT"
    assert safe_name("...") == "file"


def test_store_dedups_and_never_overwrites(conn, config):
    archive = RawArchive(config.raw_dir, conn)
    mtime = datetime(2026, 10, 6, 7, 0, tzinfo=UTC)
    first = archive.store(b"abc", source="garmin_usb", kind="monitor", original_name="A.FIT", device_mtime=mtime)
    assert first.is_new
    assert first.archive_path.exists()
    assert first.archive_path.read_bytes() == b"abc"
    assert first.archive_path.relative_to(config.raw_dir).parts[:3] == ("garmin_usb", "2026", "monitor")
    assert not (first.archive_path.stat().st_mode & 0o222)  # read-only

    again = archive.store(b"abc", source="garmin_usb", kind="monitor", original_name="B.FIT", device_mtime=mtime)
    assert not again.is_new
    assert again.raw_file_id == first.raw_file_id

    other = archive.store(b"abcd", source="garmin_usb", kind="monitor", original_name="A.FIT", year=2025)
    assert other.is_new and other.raw_file_id != first.raw_file_id
    assert "2025" in other.archive_path.parts

    rows = conn.execute("SELECT parse_status, year FROM (SELECT parse_status, substr(archive_path, 12, 4) AS year FROM raw_files)").fetchall()
    assert [r[0] for r in rows] == ["pending", "pending"]


def test_store_refuses_conflicting_file_on_disk(conn, config):
    archive = RawArchive(config.raw_dir, conn)
    stored = archive.store(b"abc", source="garmin_usb", kind="monitor", original_name="A.FIT", year=2026)
    # simulate a corrupted archive entry: same path, different content, DB row gone
    conn.execute("DELETE FROM raw_files")
    stored.archive_path.chmod(0o644)
    stored.archive_path.write_bytes(b"xyz")
    with pytest.raises(FileExistsError):
        archive.store(b"abc", source="garmin_usb", kind="monitor", original_name="A.FIT", year=2026)


def test_mark_parsed(conn, config):
    archive = RawArchive(config.raw_dir, conn)
    stored = archive.store(b"abc", source="garmin_usb", kind="monitor", original_name="A.FIT", year=2026)
    archive.mark_parsed(stored.raw_file_id, status="ok", fit_file_type="monitoring_b", device_serial="123")
    row = conn.execute("SELECT parse_status, fit_file_type, device_serial, parsed_at_utc FROM raw_files").fetchone()
    assert tuple(row)[:3] == ("ok", "monitoring_b", "123")
    assert row[3].endswith("Z")


def test_lookup_path_absolute_and_relative(conn, config):
    from pathlib import Path

    from wristpull.archive import RawArchive

    archive = RawArchive(config.raw_dir, conn)
    stored = archive.store(b"\x0e\x10data", source="garmin_usb", kind="monitor", original_name="M1.FIT", year=2026)
    assert archive.lookup_path(stored.archive_path)["id"] == stored.raw_file_id
    assert archive.lookup_path(stored.archive_path.relative_to(config.raw_dir))["sha256"] == stored.sha256
    assert archive.lookup_path(Path("/elsewhere/x.fit")) is None
    assert archive.lookup_path(config.raw_dir / "garmin_usb" / "2026" / "monitor" / "nope.fit") is None
