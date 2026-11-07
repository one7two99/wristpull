"""Phase 3 of the Connect export: activity names and the VO2max history."""

from datetime import UTC, datetime
from pathlib import Path

from connect_export import activities_json, activity_summary, build_export_zip, export_fits, maxmet_record
from wristpull.aggregate import activities_frame
from wristpull.db import table_count
from wristpull.garmin_connect import parse_activity_summaries, parse_maxmet, sport_matches
from wristpull.importers.connect import SOURCE, enrich_activity_names, import_garmin_export, store_vo2max_history
from wristpull.importers.garmin import import_garmin
from wristpull.importers.reparse import reparse
from wristpull.web.pages import activity_title
from zoneinfo import ZoneInfo

import json

RUN_START = datetime(2026, 10, 6, 6, 0, tzinfo=UTC)  # build_activity_fit's start
RUN_MS = int(RUN_START.timestamp() * 1000)


def test_parse_activity_summaries_and_sport_matching():
    data = json.dumps(activities_json([
        activity_summary("Morgenlauf", RUN_MS), activity_summary("", RUN_MS + 1), {"activityId": 1, "name": "no start"},
        activity_summary("Walk", RUN_MS - 86400000, "walking"),
    ])).encode()
    items = parse_activity_summaries(data)
    assert [i["name"] for i in items] == ["Walk", "Morgenlauf"] and items[1]["start_utc"] == "2026-10-06T06:00:00Z"
    assert items[1]["activity_type"] == "running" and items[1]["training_load"] == 42.0
    assert sport_matches("running", "running") and not sport_matches("running", "cycling")
    assert sport_matches("walking", "hiking") and sport_matches("indoor_cardio", "training")
    assert sport_matches("other", "cycling") and sport_matches(None, "cycling") and sport_matches("running", None)
    assert not sport_matches("unknown_type", "running") and sport_matches("unknown_type", "unknown_type")


def test_parse_maxmet():
    data = json.dumps([maxmet_record("2017-06-10", 44.0), maxmet_record("1989-12-31", 40.0), maxmet_record("2017-06-11", 0), {"calendarDate": "2017-06-12"}]).encode()
    rows = parse_maxmet(data, source=SOURCE, tz_name="Europe/Berlin")
    assert len(rows) == 1 and rows[0] == {"source": SOURCE, "ts_utc": "2017-06-10T05:30:27Z", "tz": "Europe/Berlin", "local_date": "2017-06-10", "metric": "vo2_max", "value": 44.0, "sport": None}


def test_enrich_names_matches_any_source_and_never_overwrites(conn, config, garmin_tree: Path):
    import_garmin(conn, config, path=garmin_tree)  # garmin_usb activity at RUN_START
    summaries = [
        {"start_utc": "2026-10-06T06:01:30Z", "activity_type": "running", "name": "Morgenlauf", "training_load": 1.0},  # within 2 min
        {"start_utc": "2026-10-06T06:00:00Z", "activity_type": "cycling", "name": "Wrong sport", "training_load": 1.0},
        {"start_utc": "2026-10-06T06:05:00Z", "activity_type": "running", "name": "Too late", "training_load": 1.0},
    ]
    assert enrich_activity_names(conn, summaries) == 1
    assert conn.execute("SELECT name, source FROM activities").fetchone()[0] == "Morgenlauf"
    assert enrich_activity_names(conn, [{"start_utc": "2026-10-06T06:00:00Z", "activity_type": None, "name": "Other name", "training_load": None}]) == 0
    assert conn.execute("SELECT name FROM activities").fetchone()[0] == "Morgenlauf"


def test_vo2max_history_keeps_fit_values(conn, config):
    rows = [{"source": SOURCE, "ts_utc": "2017-06-10T05:30:27Z", "tz": "UTC", "local_date": "2017-06-10", "metric": "vo2_max", "value": 44.0, "sport": None}]
    conn.execute("INSERT INTO raw_files (sha256, source, kind, original_name, archive_path, size_bytes, imported_at_utc) VALUES ('x', ?, 'maxmet', 'm.json', 'x/m.json', 1, 'now')", (SOURCE,))
    raw_id = conn.execute("SELECT id FROM raw_files").fetchone()[0]
    assert store_vo2max_history(conn, rows, raw_file_id=raw_id) == 1
    assert store_vo2max_history(conn, rows, raw_file_id=raw_id) == 0  # same day already there
    assert conn.execute("SELECT value, raw_file_id FROM metrics").fetchone()[1] == raw_id


def test_import_names_activities_and_vo2max_end_to_end(conn, config, tmp_path: Path):
    portal = build_export_zip(
        tmp_path / "export.zip", fits=export_fits(), junk=b"",
        json_files={
            "DI-Connect-Fitness/alice@example.com_1_summarizedActivities.json": activities_json([activity_summary("Morgenlauf", RUN_MS)]),
            "DI-Connect-Metrics/MetricsMaxMetData_20170610_20170918_1.json": [maxmet_record("2017-06-10", 44.0), maxmet_record("2026-10-06", 50.0)],
        },
    )
    stats = import_garmin_export(conn, config, portal)
    assert stats.errors == 0 and stats.rows["activity_names"] == 1
    row = conn.execute("SELECT name, profile_name, sport FROM activities WHERE source = ?", (SOURCE,)).fetchone()
    assert row[0] == "Morgenlauf"
    assert activity_title(dict(row)) == "Morgenlauf"
    vo2 = conn.execute("SELECT local_date, value FROM metrics WHERE metric = 'vo2_max' ORDER BY local_date").fetchall()
    assert ("2017-06-10", 44.0) in [tuple(r) for r in vo2]
    frame = activities_frame(conn, "2026-10-06", "2026-10-06", ZoneInfo("Europe/Berlin"))
    assert frame["name"][0] == "Morgenlauf"
    kinds = {r[0] for r in conn.execute("SELECT kind FROM raw_files WHERE kind IN ('activities_json', 'maxmet')")}
    assert kinds == {"activities_json", "maxmet"}
    archive = conn.execute("SELECT archive_path FROM raw_files WHERE kind = 'maxmet'").fetchone()[0]
    assert archive.startswith("garmin_connect/2017/maxmet/")
    before = table_count(conn, "metrics")
    assert reparse(conn, config, source=SOURCE, kind="maxmet").ok == 1 and table_count(conn, "metrics") == before
    assert reparse(conn, config, source=SOURCE, kind="activities_json").ok == 1
    assert conn.execute("SELECT name FROM activities WHERE source = ?", (SOURCE,)).fetchone()[0] == "Morgenlauf"
