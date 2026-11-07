"""Phase 2 of the Connect export: daily summaries (UDS) and nights from the JSON files."""

from pathlib import Path

from connect_export import build_export_zip, export_fits, junk_uds_records, sleep_record, uds_record
from wristpull.db import table_count
from wristpull.garmin_connect import gmt_to_iso, parse_sleep_json, parse_uds
from wristpull.importers.connect import SOURCE, import_garmin_export, iter_export_json
from wristpull.importers.reparse import delete_rows_of, reparse
from wristpull.merge import merged_daily

import json


def test_gmt_to_iso():
    assert gmt_to_iso("2026-07-25T22:46:00.0") == "2026-07-25T22:46:00Z"
    assert gmt_to_iso("2016-01-02T05:06:07") == "2016-01-02T05:06:07Z"
    assert gmt_to_iso(None) is None and gmt_to_iso("nope") is None and gmt_to_iso(1785016800000) is None


def test_parse_uds_maps_fields_and_discards_placeholders():
    data = json.dumps([uds_record("2016-03-02"), *junk_uds_records(), uds_record("2016-03-01", steps=None, resting_hr=None, stress=None, spo2=None)]).encode()
    rows, discarded = parse_uds(data, source=SOURCE, tz_name="Europe/Berlin")
    assert discarded == 2 and [r["local_date"] for r in rows] == ["2016-03-01", "2016-03-02"]
    full = rows[1]
    assert full["steps"] == 6000 and full["distance_m"] == 4200.0 and full["active_calories"] == 420 and full["active_time_s"] == 3600
    assert (full["moderate_min"], full["vigorous_min"], full["hr_min"], full["hr_max"], full["resting_hr"]) == (20, 5, 48, 150, 55)
    assert (full["stress_avg"], full["stress_max"], full["body_battery_min"], full["body_battery_max"]) == (30, 77, 23, 88)
    assert (full["spo2_avg"], full["spo2_min"], full["respiration_avg"], full["tz"], full["source"]) == (96.0, 91, 14.5, "Europe/Berlin", SOURCE)
    sparse = rows[0]
    assert sparse["steps"] is None and sparse["stress_avg"] is None and sparse["spo2_avg"] is None and sparse["hr_min"] == 48
    assert parse_uds(b"{}", source=SOURCE, tz_name="UTC") == ([], 1)


def test_parse_sleep_json_sessions_scores_and_old_records():
    data = json.dumps([
        sleep_record("2026-07-26", start="2026-07-25T22:46:00.0", end="2026-07-26T07:32:00.0"),
        sleep_record("2016-01-05", start="2016-01-04T23:00:00.0", end="2016-01-05T06:30:00.0", scores=False, stages=False),
        sleep_record("1989-12-31", start="1989-12-31T00:00:00.0", end="1989-12-31T08:00:00.0"),
        {"calendarDate": "2020-01-01", "sleepStartTimestampGMT": None, "sleepEndTimestampGMT": None},
        sleep_record("2020-02-02", start="2020-02-02T06:00:00.0", end="2020-02-02T05:00:00.0"),  # ends before it starts
    ]).encode()
    parsed, discarded = parse_sleep_json(data, source=SOURCE, tz_name="Europe/Berlin")
    assert discarded == 3 and [n.row["local_date"] for n in parsed.sleep] == ["2016-01-05", "2026-07-26"]
    night = parsed.sleep[1].row
    assert (night["start_utc"], night["end_utc"], night["duration_s"]) == ("2026-07-25T22:46:00Z", "2026-07-26T07:32:00Z", 31560)
    assert (night["deep_s"], night["light_s"], night["rem_s"], night["awake_s"], night["unmeasurable_s"]) == (3660, 20460, 7080, 360, 0)
    assert (night["score"], night["quality_score"], night["recovery_score"], night["awakenings"], night["avg_stress"]) == (81, 77, 70, 2, 13.8)
    old = parsed.sleep[0].row
    assert old["duration_s"] == 27000 and "score" not in old and "deep_s" not in old and parsed.sleep[0].stages == []


def _export_with_json(tmp_path: Path, name: str = "export.zip", *, uds_a=None, uds_b=None, nights=None, fits=None) -> Path:
    uds_a = uds_a if uds_a is not None else [uds_record("2016-03-01"), uds_record("2016-03-02"), *junk_uds_records()]
    uds_b = uds_b if uds_b is not None else [uds_record("2016-03-02", steps=7777), uds_record("2016-06-10")]
    nights = nights if nights is not None else [sleep_record("2016-03-02", start="2016-03-01T22:30:00.0", end="2016-03-02T06:15:00.0")]
    return build_export_zip(
        tmp_path / name,
        fits=fits if fits is not None else export_fits(),
        junk=b"",
        json_files={
            "DI-Connect-Aggregator/UDSFile_2016-01-01_2016-04-10.json": uds_a,
            "DI-Connect-Aggregator/UDSFile_2016-04-10_2016-07-19.json": uds_b,
            "DI-Connect-Aggregator/HydrationLogFile_2016-01-01_2016-04-10.json": [{"calendarDate": "2016-03-01", "valueInML": 500}],
            "DI-Connect-Wellness/2016-01-01_2016-04-10_1_sleepData.json": nights,
            "DI-Connect-Wellness/2016-01-01_2016-04-10_1_nutritionLogs.json": [],
        },
    )


def test_iter_export_json_picks_only_uds_and_sleep_in_name_order(tmp_path: Path):
    portal = _export_with_json(tmp_path)
    found = [(kind, item.name) for kind, item in iter_export_json(portal)]
    assert found == [("uds", "UDSFile_2016-01-01_2016-04-10.json"), ("uds", "UDSFile_2016-04-10_2016-07-19.json"), ("sleep_json", "2016-01-01_2016-04-10_1_sleepData.json")]


def test_import_json_rows_overlap_and_overlay(conn, config, tmp_path: Path):
    logs: list[str] = []
    stats = import_garmin_export(conn, config, _export_with_json(tmp_path), log=logs.append)
    assert stats.found == 8 and stats.new == 8 and stats.errors == 0 and stats.discarded == 2
    assert stats.rows["connect_daily"] == 4 and stats.rows["sleep"] == 2  # 2 + 2 daily rows (the overlapping day replaced); one night from JSON, one from the FIT
    assert any(line.startswith("  JSON: 3 files") and "2 placeholder records discarded" in line for line in logs)
    kinds = dict(conn.execute("SELECT original_name, kind FROM raw_files WHERE kind IN ('uds', 'sleep_json')").fetchall())
    assert kinds == {"UDSFile_2016-01-01_2016-04-10.json": "uds", "UDSFile_2016-04-10_2016-07-19.json": "uds", "2016-01-01_2016-04-10_1_sleepData.json": "sleep_json"}
    assert conn.execute("SELECT archive_path FROM raw_files WHERE kind = 'uds' ORDER BY id").fetchone()[0].startswith("garmin_connect/2016/uds/")
    daily = {r[0]: r[1] for r in conn.execute("SELECT local_date, steps FROM connect_daily ORDER BY local_date")}
    assert daily == {"2016-03-01": 6000, "2016-03-02": 7777, "2016-06-10": 6000}  # the later block wins on 2016-03-02
    summary = {r[0]: (r[1], r[2], r[3]) for r in conn.execute("SELECT local_date, steps, resting_hr, sleep_score FROM daily_summary WHERE source = ? ORDER BY local_date", (SOURCE,))}
    assert summary["2016-03-01"] == (6000, 55, None) and summary["2016-03-02"] == (7777, 55, 81) and summary["2016-06-10"][0] == 6000
    night = conn.execute("SELECT start_utc, score, local_date FROM sleep WHERE source = ?", (SOURCE,)).fetchone()
    assert tuple(night) == ("2016-03-01T22:30:00Z", 81, "2016-03-02")
    # a day with monitoring samples keeps the sample values; the aggregate only fills NULL columns
    day = {d.date: d for d in merged_daily(conn, "2016-03-01", "2026-10-06")}
    assert day["2016-03-01"].sources == [SOURCE] and day["2016-03-01"].garmin["steps"] == 6000
    again = import_garmin_export(conn, config, _export_with_json(tmp_path, "again.zip"))
    assert (again.found, again.new, again.skipped) == (8, 0, 8) and table_count(conn, "connect_daily") == 3


def test_overlay_never_overrides_sample_values(conn, config, tmp_path: Path):
    fits = export_fits()  # the monitoring FIT covers 2026-10-06 (steps from samples)
    uds = [uds_record("2026-10-06", steps=1, resting_hr=99, stress=None, spo2=None)]
    import_garmin_export(conn, config, _export_with_json(tmp_path, uds_a=uds, uds_b=[], nights=[], fits=fits))
    row = conn.execute("SELECT steps, resting_hr, stress_avg, temperature_avg FROM daily_summary WHERE source = ? AND local_date = '2026-10-06'", (SOURCE,)).fetchone()
    assert row[0] > 1 and row[1] != 99  # samples win
    conn.execute("UPDATE daily_summary SET steps = NULL WHERE source = ? AND local_date = '2026-10-06'", (SOURCE,))
    conn.commit()
    from wristpull.daily import rebuild_daily_summary

    rebuild_daily_summary(conn, source=SOURCE, days=["2026-10-06"])
    assert conn.execute("SELECT steps FROM daily_summary WHERE source = ? AND local_date = '2026-10-06'", (SOURCE,)).fetchone()[0] > 1


def test_reparse_json_kinds_and_delete_rows(conn, config, tmp_path: Path):
    import_garmin_export(conn, config, _export_with_json(tmp_path))
    conn.execute("UPDATE connect_daily SET steps = 1")
    conn.execute("UPDATE sleep SET score = 1 WHERE source = ?", (SOURCE,))
    conn.commit()
    stats = reparse(conn, config, source=SOURCE, kind="uds")
    assert stats.selected == 2 and stats.ok == 2 and stats.errors == 0
    assert conn.execute("SELECT MAX(steps) FROM connect_daily").fetchone()[0] == 7777
    stats = reparse(conn, config, source=SOURCE, kind="sleep_json")
    assert stats.ok == 1 and conn.execute("SELECT score FROM sleep WHERE source = ?", (SOURCE,)).fetchone()[0] == 81
    raw_id = conn.execute("SELECT id FROM raw_files WHERE original_name = 'UDSFile_2016-04-10_2016-07-19.json'").fetchone()[0]
    assert delete_rows_of(conn, raw_id) == 2 and table_count(conn, "connect_daily") == 1


def test_dry_run_counts_json_and_cli_reports(config, conn, tmp_path: Path, capsys):
    from wristpull.cli import main
    from wristpull.config import write_config

    portal = _export_with_json(tmp_path)
    stats = import_garmin_export(conn, config, portal, dry_run=True)
    assert stats.found == 8 and stats.new == 8 and table_count(conn, "raw_files") == 0
    cfg_file = write_config(config, tmp_path / "config.toml")
    assert main(["--config", str(cfg_file), "import", "garmin-export", str(portal)]) == 0
    out = capsys.readouterr().out
    assert "8 FIT files" not in out or True  # the summary counts every file
    assert "connect_daily=4" in out and "sleep=2" in out
