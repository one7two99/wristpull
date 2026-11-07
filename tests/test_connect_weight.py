"""Weight history from the Connect export's biometrics file, and the trend charts for weight and VO2max."""

from datetime import date
from pathlib import Path

from connect_export import biometrics_record, build_export_zip, export_fits, maxmet_record
from wristpull.garmin_connect import parse_biometrics
from wristpull.importers.connect import SOURCE, import_garmin_export
from wristpull.importers.reparse import reparse
from wristpull.merge import merged_metric_by_day
from wristpull.web import pages

import json


def test_parse_biometrics_takes_the_last_entry_per_day_in_kg():
    data = json.dumps([
        biometrics_record("2018-03-01", 68100.0), biometrics_record("2018-03-01", 68500.0, sequence=2), biometrics_record("2018-03-02", None, vo2=48.0),
        biometrics_record("2013-08-24", 61400.0), biometrics_record("1989-12-31", 70000.0), biometrics_record("2018-03-03", 0),
        {"version": 1, "weight": {"weight": 70000.0}},  # no date: the static profile value
    ]).encode()
    rows = parse_biometrics(data, source=SOURCE, tz_name="Europe/Berlin")
    assert [(r["local_date"], r["value"]) for r in rows] == [("2013-08-24", 61.4), ("2018-03-01", 68.5)]
    assert rows[0]["metric"] == "weight_kg" and rows[0]["ts_utc"] == "2013-08-24T12:00:00Z" and rows[0]["sport"] is None


def test_import_weight_history_reparse_and_merged_series(conn, config, tmp_path: Path):
    portal = build_export_zip(
        tmp_path / "export.zip", fits=export_fits(), junk=b"",
        json_files={
            "DI-Connect-Wellness/1_userBioMetrics.json": [biometrics_record("2026-09-20", 72000.0), biometrics_record("2026-10-01", 71500.0)],
            "DI-Connect-Wellness/1_userBioMetricProfileData.json": [{"height": 180.0, "weight": 71000.0}],
            "DI-Connect-Metrics/MetricsMaxMetData_20260901_20261001_1.json": [maxmet_record("2026-09-25", 46.0)],
        },
    )
    stats = import_garmin_export(conn, config, portal)
    assert stats.errors == 0 and stats.rows["metrics"] >= 3
    kinds = {r[0] for r in conn.execute("SELECT kind FROM raw_files WHERE source = ?", (SOURCE,))}
    assert "biometrics" in kinds and "uds" not in kinds
    weights = conn.execute("SELECT local_date, value FROM metrics WHERE metric = 'weight_kg' ORDER BY local_date").fetchall()
    assert [tuple(r) for r in weights] == [("2026-09-20", 72.0), ("2026-10-01", 71.5)]
    assert merged_metric_by_day(conn, "2026-09-01", "2026-10-31", "weight_kg") == {"2026-09-20": 72.0, "2026-10-01": 71.5}
    # a higher-priority source wins the day
    conn.execute("INSERT INTO metrics (source, ts_utc, tz, local_date, metric, value) VALUES ('garmin_usb', '2026-10-01T06:00:00Z', 'UTC', '2026-10-01', 'weight_kg', 70.0)")
    conn.commit()
    assert merged_metric_by_day(conn, "2026-10-01", "2026-10-01", "weight_kg") == {"2026-10-01": 70.0}
    before = conn.execute("SELECT COUNT(*) FROM metrics WHERE metric = 'weight_kg'").fetchone()[0]
    assert reparse(conn, config, source=SOURCE, kind="biometrics").ok == 1
    assert conn.execute("SELECT COUNT(*) FROM metrics WHERE metric = 'weight_kg'").fetchone()[0] == before
    trends = pages.trends_data(conn, date(2026, 10, 6), 4)
    ids = [c["id"] for c in trends["charts"]]
    assert ids[-2:] == ["weight", "vo2max"]
    weight = next(c for c in trends["charts"] if c["id"] == "weight")
    assert weight["kind"] == "points" and weight["unit"] == "kg"
    assert weight["series"][0]["data"][weight["labels"].index("2026-10-01")] == 70.0 and weight["series"][0]["data"][0] is None
    assert pages.trends_data(conn, date(2020, 1, 1), 4)["charts"][-1]["id"] == "acwr"  # no metrics in that window: no empty charts
