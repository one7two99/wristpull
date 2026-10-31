from pathlib import Path
from zoneinfo import ZoneInfo

from gb_db import STRAP_MAC, build_gb_db
from wristpull.gadgetbridge import GbDevice, parse_gadgetbridge_db

TZ = ZoneInfo("Europe/Berlin")


def test_device_source_mapping():
    assert GbDevice(1, "", None, "x", "GARMIN_FENIX_6X", None).source == "gadgetbridge_garmin"
    assert GbDevice(2, "", None, "x", "AMAZFITHELIOSTRAP", None).source == "gadgetbridge_zepp"
    assert GbDevice(3, "", None, "x", "PEBBLE", None).source is None


def test_parse_strap_rows(tmp_path: Path):
    db = build_gb_db(tmp_path / "Gadgetbridge.db")
    rows, devices = parse_gadgetbridge_db(db, TZ)
    assert [d.mac for d in devices][1] == STRAP_MAC
    counts = rows.counts()
    # HR: 300 minutes minus 5 invalid (255)
    assert counts["heart_rate"] == 295
    assert rows.heart_rate[0]["ts_utc"] == "2026-10-31T20:05:00Z" and rows.heart_rate[0]["bpm"] == 75
    assert all(r["source"] == "gadgetbridge_zepp" for r in rows.heart_rate)
    # steps are converted to a cumulative per-day counter with activity_type 'all'
    assert "steps" in rows.replace_tables
    day1 = [r for r in rows.steps if r["local_date"] == "2026-10-31"]
    day2 = [r for r in rows.steps if r["local_date"] == "2026-11-01"]
    assert day1[-1]["steps"] == sum(12 for i in range(5, 180) if i % 3 == 0 and not 120 <= i < 180)
    assert day2[0]["steps"] == 0 and day2[-1]["steps"] > 0
    assert all(r["activity_type"] == "all" for r in rows.steps)
    # sleep from the per-minute flags: 90 minutes, 30 deep, 10 rem, rest light
    assert len(rows.sleep) == 1
    night = rows.sleep[0]
    assert night.row["start_utc"] == "2026-10-31T22:00:00Z" and night.row["end_utc"] == "2026-10-31T23:30:00Z"
    assert night.row["local_date"] == "2026-11-01"
    assert night.row["deep_s"] == 30 * 60 and night.row["rem_s"] == 10 * 60 and night.row["light_s"] == 50 * 60
    assert night.row["awake_s"] == 0 and night.row["duration_s"] == 90 * 60
    assert [s["stage"] for s in night.stages] == ["light", "deep", "light", "rem", "light"]
    # other samples (millisecond timestamps)
    assert counts["stress"] == 60 and rows.stress[0]["level"] == 15
    assert counts["temperature"] == 60 and rows.temperature[0] == {"source": "gadgetbridge_zepp", "ts_utc": "2026-10-31T20:00:00Z", "tz": "Europe/Berlin", "local_date": "2026-10-31", "celsius": 30.0, "kind": "skin"}
    assert rows.spo2[0]["spo2_pct"] == 97 and rows.resting_hr[0]["resting_hr"] == 51
    assert rows.respiration[0]["breaths_per_min"] == 14.0 and rows.hrv_values[0]["rmssd_ms"] == 45.0
    assert {m["metric"]: m["value"] for m in rows.metrics} == {"pai_total": 41.5, "pai_today": 3.0}
    # workout from BASE_ACTIVITY_SUMMARY
    assert len(rows.activities) == 1
    act = rows.activities[0].row
    assert act["sport"] == "walking" and act["profile_name"] == "Evening walk"
    assert act["start_utc"] == "2026-10-31T20:30:00Z" and act["elapsed_s"] == 26 * 60 and act["timer_s"] == 1500
    assert act["distance_m"] == 3200.0 and act["calories"] == 210 and act["avg_hr"] == 128 and act["max_hr"] == 151
    assert act["device_serial"] == STRAP_MAC
    # Garmin rows in the database are ignored
    assert not any(r["source"] == "gadgetbridge_garmin" for r in rows.stress)
