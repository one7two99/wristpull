import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from conftest import build_activity_fit, build_hrv_fit, build_metrics_fit, build_monitor_fit, build_sleep_fit, file_id, ts
from wristpull.fit.decode import decode_fit, summarize
from wristpull.fit.garmin import compute_timestamp16, parse_fit

TZ = ZoneInfo("Europe/Berlin")
SRC = "garmin_usb"


def test_compute_timestamp16_rollover():
    ref = datetime(2026, 10, 5, 22, 0, tzinfo=UTC)
    later = ref + timedelta(minutes=2)
    from wristpull.timeutil import utc_to_fit

    assert compute_timestamp16(utc_to_fit(later) & 0xFFFF, ref) == later
    # the 16-bit field is ambiguous beyond +-9 h; differences wrap to the nearest instant
    far = ref + timedelta(seconds=65000)
    assert compute_timestamp16(utc_to_fit(far) & 0xFFFF, ref) == ref - timedelta(seconds=536)
    earlier = ref - timedelta(seconds=30)
    assert compute_timestamp16(utc_to_fit(earlier) & 0xFFFF, ref) == earlier


def test_monitor_file_rows():
    fit = decode_fit(build_monitor_fit())
    assert fit.file_type == "monitoring_b"
    rows = parse_fit(fit, TZ, SRC)
    counts = rows.counts()
    assert counts["heart_rate"] == 96 * 7
    hr = rows.heart_rate[0]
    assert hr["ts_utc"] == "2026-10-05T22:02:00Z"
    assert hr["local_date"] == "2026-10-06" and hr["tz"] == "Europe/Berlin"
    # steps: cumulative per activity type; midnight record counts for the previous day
    walk = [r for r in rows.steps if r["activity_type"] == "walking"]
    assert walk[0]["steps"] == 0 and walk[0]["local_date"] == "2026-10-05"  # 00:00 local record -> previous day
    assert max(r["steps"] for r in walk if r["local_date"] == "2026-10-06") == 52 * 120
    assert any(r["activity_type"] == "running" and r["steps"] == 2000 for r in rows.steps)
    assert walk[1]["distance_m"] == 0.0 and walk[30]["distance_m"] > 0
    # stress and body battery from the same message
    assert counts["stress"] == 96
    assert counts["body_battery"] == 96
    assert rows.stress[0]["level"] == -1
    assert rows.body_battery[0]["level"] == 90
    assert rows.body_battery[0]["ts_utc"] == "2026-10-05T22:00:00Z"
    assert rows.resting_hr == [
        {"source": SRC, "ts_utc": "2026-10-06T21:00:00Z", "tz": "Europe/Berlin", "local_date": "2026-10-06", "resting_hr": 48, "current_day_resting_hr": 47}
    ]
    assert rows.spo2[0]["spo2_pct"] == 96 and rows.spo2[0]["mode"] in ("3", "periodic", "spot_check", "continuous_check")
    assert rows.respiration[0]["breaths_per_min"] == 14.5
    assert rows.intensity_minutes == [
        {"source": SRC, "ts_utc": "2026-10-06T18:00:00Z", "tz": "Europe/Berlin", "local_date": "2026-10-06", "moderate": 25, "vigorous": 10}
    ]
    assert not rows.activities and not rows.sleep


def test_sleep_file_rows():
    rows = parse_fit(decode_fit(build_sleep_fit()), TZ, SRC)
    assert len(rows.sleep) == 1
    night = rows.sleep[0]
    # the sleep window (event 74) opens 20 minutes before the first stage record
    assert night.row["start_utc"] == "2026-10-05T21:10:00Z"
    assert night.row["end_utc"] == "2026-10-06T05:20:00Z"
    assert night.row["local_date"] == "2026-10-06"
    assert night.row["awake_s"] == (10 + 5) * 60
    assert night.row["deep_s"] == (50 + 40) * 60
    assert night.row["rem_s"] == 30 * 60
    assert night.row["light_s"] == (30 + 30 + 150 + 80 + 45) * 60
    assert night.row["unmeasurable_s"] == 20 * 60
    assert night.row["duration_s"] == 490 * 60 - night.row["awake_s"]
    assert night.row["score"] == 78 and night.row["awakenings"] == 2 and night.row["avg_stress"] == 21.5
    assert len(night.stages) == 12
    assert night.stages[0] == {"start_utc": "2026-10-05T21:10:00Z", "end_utc": "2026-10-05T21:30:00Z", "stage": "unmeasurable"}
    assert night.stages[1] == {"start_utc": "2026-10-05T21:30:00Z", "end_utc": "2026-10-05T21:40:00Z", "stage": "awake"}
    assert night.stages[-1]["stage"] == "awake" and night.stages[-1]["start_utc"] == night.stages[-1]["end_utc"]


def test_activity_file_rows():
    rows = parse_fit(decode_fit(build_activity_fit()), TZ, SRC)
    assert len(rows.activities) == 1
    act = rows.activities[0]
    row = act.row
    assert row["start_utc"] == "2026-10-06T06:00:00Z" and row["end_utc"] == "2026-10-06T06:30:00Z"
    assert row["local_date"] == "2026-10-06"
    assert row["sport"] == "running" and row["sub_sport"] == "street" and row["profile_name"] == "Run"
    assert row["elapsed_s"] == 1800.0 and row["distance_m"] == 5040.0
    assert row["avg_hr"] == 141 and row["max_hr"] == 168
    assert row["avg_speed_mps"] == 2.8 and row["max_speed_mps"] == 3.9
    assert row["aerobic_te"] == 3.3 and row["anaerobic_te"] == 1.2
    assert row["training_load"] == 61.5
    assert row["ascent_m"] == 45 and row["num_laps"] == 3
    assert json.loads(row["hr_zone_times_s"]) == [0, 120, 900, 600, 180, 0]
    assert row["device_serial"] == "3999999999"
    assert len(act.records) == 360
    rec = act.records[1]
    assert rec["ts_utc"] == "2026-10-06T06:00:05Z"
    assert rec["speed_mps"] == 2.8 and rec["distance_m"] == 14.0 and rec["hr"] == 120
    assert abs(rec["lat"] - 52.52005) < 1e-4 and abs(rec["lon"] - 13.40505) < 1e-4
    assert rec["altitude_m"] == 540.0 and rec["temperature_c"] == 14.0
    assert [lap["lap_index"] for lap in act.laps] == [0, 1, 2]
    assert act.laps[1]["start_utc"] == "2026-10-06T06:10:00Z" and act.laps[1]["avg_hr"] == 141
    assert not rows.heart_rate  # activity HR lives in activity_records, not the wellness table


def test_metrics_and_hrv_rows():
    metrics = parse_fit(decode_fit(build_metrics_fit()), TZ, SRC)
    assert metrics.metrics == [
        {"source": SRC, "ts_utc": "2026-10-06T06:40:00Z", "tz": "Europe/Berlin", "local_date": "2026-10-06", "metric": "vo2_max", "value": 52.3, "sport": "running"}
    ]
    hrv = parse_fit(decode_fit(build_hrv_fit()), TZ, SRC)
    assert len(hrv.hrv_values) == 6 and hrv.hrv_values[0]["rmssd_ms"] == 38.0
    assert hrv.hrv[0]["last_night_avg_ms"] == 40.0 and hrv.hrv[0]["status"] == "balanced"


def test_summarize_lists_unknown_fields():
    summary = summarize(decode_fit(build_monitor_fit()))
    assert summary["stress_level"]["count"] == 96
    keys = list(summary["stress_level"]["fields"])
    assert "0:stress_level_value" in keys and "1:stress_level_time" in keys
    assert "3:unknown_3" in keys  # body battery, undocumented in the SDK profile
    assert "19:active_calories" in summary["monitoring"]["fields"]


def test_sleep_windows_and_unmatched_events():
    from datetime import UTC, datetime, timedelta

    from fitwriter import FitWriter
    from wristpull.fit.garmin import sleep_windows

    start = datetime(2026, 10, 5, 22, 0, tzinfo=UTC)
    w = FitWriter()
    file_id(w, 49, start)
    w.message(21, [(253, "uint32", ts(start)), (0, "enum", 74), (1, "enum", 0)])  # start without stop
    w.message(21, [(253, "uint32", ts(start + timedelta(hours=1))), (0, "enum", 74), (1, "enum", 0)])
    w.message(21, [(253, "uint32", ts(start + timedelta(hours=2))), (0, "enum", 0), (1, "enum", 1)])  # other event
    w.message(21, [(253, "uint32", ts(start + timedelta(hours=8))), (0, "enum", 74), (1, "enum", 1)])
    fit = decode_fit(w.finish())
    assert sleep_windows(fit) == [(start + timedelta(hours=1), start + timedelta(hours=8))]
    # a sleep file without stage records yields no night, window or not
    assert parse_fit(fit, TZ, SRC).sleep == []
