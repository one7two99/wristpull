from pathlib import Path

from wristpull.daily import rebuild_daily_summary
from wristpull.db import table_count
from wristpull.importers.garmin import import_garmin


def test_import_from_tree_and_idempotent(conn, config, garmin_tree: Path):
    logs: list[str] = []
    stats = import_garmin(conn, config, path=garmin_tree.parent, log=logs.append)
    assert stats.root == garmin_tree
    assert (stats.found, stats.new, stats.skipped, stats.errors) == (5, 5, 0, 0)
    assert stats.rows["activities"] == 1 and stats.rows["sleep"] == 1
    assert stats.rows["heart_rate"] == 672 and stats.rows["body_battery"] == 96
    assert any("Monitor/M6A10500.FIT" in line for line in logs)

    # archive layout and immutability
    archived = sorted(p.relative_to(config.raw_dir).as_posix() for p in config.raw_dir.rglob("*") if p.is_file())
    assert len(archived) == 5
    assert archived[0].startswith("garmin_usb/2026/activity/")
    assert all(p.split("/")[1] == "2026" for p in archived)
    assert table_count(conn, "raw_files", "parse_status = 'ok'") == 5
    row = conn.execute("SELECT fit_file_type, device_serial, device_product FROM raw_files WHERE kind = 'activity'").fetchone()
    assert tuple(row) == ("activity", "3999999999", "fenix6_sport")

    # second run: nothing new, nothing duplicated
    again = import_garmin(conn, config, path=garmin_tree)
    assert (again.found, again.new, again.skipped) == (5, 0, 5)
    assert table_count(conn, "heart_rate") == 672
    assert table_count(conn, "activities") == 1
    assert table_count(conn, "sleep_stages") == 11

    # daily summary was rebuilt
    day = conn.execute("SELECT * FROM daily_summary WHERE local_date = '2026-10-06'").fetchone()
    assert day["steps"] == 52 * 120 + 2000
    assert day["distance_m"] == 52 * 120 * 0.7 + 2000 * 1.1
    assert day["hr_min"] == 56 and day["hr_max"] == 55 + 5 * 3 + 7 + 70  # i in 44..47 -> i % 7 max 5
    assert day["resting_hr"] == 47
    assert day["stress_max"] <= 79 and day["stress_avg"] > 0
    assert day["body_battery_min"] == 50 and day["body_battery_max"] == 90
    assert day["spo2_avg"] == 96.0 and day["respiration_avg"] == 14.5
    assert day["sleep_duration_s"] == 470 * 60 - 15 * 60 and day["sleep_score"] == 78
    assert day["moderate_min"] == 25 and day["vigorous_min"] == 10
    assert day["activities_count"] == 1 and day["training_load"] == 61.5
    assert day["hrv_last_night_avg_ms"] == 40.0
    prev = conn.execute("SELECT steps FROM daily_summary WHERE local_date = '2026-10-05'").fetchone()
    assert prev["steps"] == 0  # only the midnight carry-over record


def test_dry_run_copies_nothing(conn, config, garmin_tree: Path):
    stats = import_garmin(conn, config, path=garmin_tree, dry_run=True)
    assert stats.new == 5 and stats.found == 5
    assert not config.raw_dir.exists()
    assert table_count(conn, "raw_files") == 0


def test_no_device_returns_empty_root(conn, config, tmp_path: Path):
    config.garmin_mount_hints = [tmp_path / "nothing"]
    stats = import_garmin(conn, config, path=tmp_path / "nothing")
    assert stats.root is None and stats.found == 0


def test_corrupt_file_is_archived_with_error(conn, config, garmin_tree: Path):
    (garmin_tree / "Monitor" / "BROKEN.FIT").write_bytes(b"\x0e\x10\x00\x00garbage" * 10)
    stats = import_garmin(conn, config, path=garmin_tree)
    assert stats.errors == 1 and stats.parsed_ok == 5
    row = conn.execute("SELECT parse_status, parse_error, archive_path FROM raw_files WHERE original_name = 'BROKEN.FIT'").fetchone()
    assert row["parse_status"] == "error" and row["parse_error"]
    assert (config.raw_dir / row["archive_path"]).exists()
    assert "unknown" in row["archive_path"] or "2026" in row["archive_path"]


def test_rebuild_daily_summary_subset(conn, config, garmin_tree: Path):
    import_garmin(conn, config, path=garmin_tree)
    conn.execute("DELETE FROM daily_summary")
    assert rebuild_daily_summary(conn, source="garmin_usb", days=["2026-10-06"]) == 1
    assert table_count(conn, "daily_summary") == 1
