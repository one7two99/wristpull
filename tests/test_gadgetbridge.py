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
    # sleep from the band's session report: 90 minutes, 30 deep, 10 rem, rest light, score 70
    assert len(rows.sleep) == 1
    night = rows.sleep[0]
    assert night.row["start_utc"] == "2026-10-31T22:00:00Z" and night.row["end_utc"] == "2026-10-31T23:30:00Z"
    assert night.row["local_date"] == "2026-11-01"
    assert night.row["deep_s"] == 30 * 60 and night.row["rem_s"] == 10 * 60 and night.row["light_s"] == 50 * 60
    assert night.row["awake_s"] == 0 and night.row["duration_s"] == 90 * 60
    assert night.row["score"] == 70 and night.row["awakenings"] == 0
    assert [s["stage"] for s in night.stages] == ["light", "deep", "light", "rem", "light"]
    assert night.stages[1] == {"start_utc": "2026-10-31T22:30:00Z", "end_utc": "2026-10-31T23:00:00Z", "stage": "deep"}
    # other samples (millisecond timestamps)
    assert counts["stress"] == 60 and rows.stress[0]["level"] == 15
    assert counts["temperature"] == 60 and rows.temperature[0] == {"source": "gadgetbridge_zepp", "ts_utc": "2026-10-31T20:00:00Z", "tz": "Europe/Berlin", "local_date": "2026-10-31", "celsius": 30.0, "kind": "skin"}
    assert rows.spo2[0]["spo2_pct"] == 97 and rows.resting_hr[0]["resting_hr"] == 51
    assert rows.respiration[0]["breaths_per_min"] == 14.0 and rows.hrv_values[0]["rmssd_ms"] == 45.0
    assert {m["metric"]: m["value"] for m in rows.metrics} == {"pai_total": 41.5, "pai_today": 3.0}
    # workout from BASE_ACTIVITY_SUMMARY; the 3-minute accidental start (kind 0, no values) is skipped
    assert len(rows.activities) == 1
    act = rows.activities[0].row
    assert act["sport"] == "walking" and act["profile_name"] == "Evening walk"
    assert act["start_utc"] == "2026-10-31T20:30:00Z" and act["elapsed_s"] == 26 * 60 and act["timer_s"] == 1500
    assert act["distance_m"] == 3200.0 and act["calories"] == 210 and act["avg_hr"] == 128 and act["max_hr"] == 151
    assert act["device_serial"] == STRAP_MAC
    # Garmin rows in the database are ignored
    assert not any(r["source"] == "gadgetbridge_garmin" for r in rows.stress)


def test_sleep_falls_back_to_minute_flags_without_session(tmp_path: Path):
    db = build_gb_db(tmp_path / "Gadgetbridge.db", sleep_session=False)
    rows, _ = parse_gadgetbridge_db(db, TZ)
    assert len(rows.sleep) == 1
    night = rows.sleep[0]
    assert night.row["start_utc"] == "2026-10-31T22:00:00Z" and night.row["end_utc"] == "2026-10-31T23:30:00Z"
    assert night.row["deep_s"] == 30 * 60 and night.row["rem_s"] == 10 * 60 and night.row["light_s"] == 50 * 60
    assert night.row["score"] is None
    assert [s["stage"] for s in night.stages] == ["light", "deep", "light", "rem", "light"]


def test_huami_flag_stage_thresholds():
    from wristpull.gadgetbridge import huami_flag_stage

    assert huami_flag_stage(80, 128, 128) is None  # not asleep
    assert huami_flag_stage(120, 128, 128) == "light"  # bit 7 only: no stage information
    assert huami_flag_stage(120, 128 + 43, 128) == "deep"
    assert huami_flag_stage(120, 128 + 42, 128) == "light"
    assert huami_flag_stage(120, 128 + 60, 128 + 56) == "rem"  # rem wins over deep
    assert huami_flag_stage(120, None, None) == "light"


def test_decode_sleep_session_and_revisions():
    from datetime import UTC, datetime, timedelta

    from gb_db import build_sleep_session_blob
    from wristpull.gadgetbridge import sleep_from_sessions, decode_sleep_session

    midnight = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)
    assert decode_sleep_session(b"\x00" * 10) is None
    assert decode_sleep_session(build_sleep_session_blob(reported=midnight, midnight=midnight, stages=[])) is None
    first = build_sleep_session_blob(reported=midnight, midnight=midnight, stages=[(1440, 1499, "light"), (1500, 1500, "awake")], score=50)
    decoded = decode_sleep_session(first)
    assert decoded["score"] == 50 and decoded["avg_hr"] is None
    assert decoded["stages"][0][0] == midnight and decoded["stages"][0][1] == midnight + timedelta(minutes=60)
    assert decoded["totals_min"] == {"rem": 0, "light": 60, "deep": 0, "awake": 1}
    # a later overlapping report revises the first; a disjoint one is a second session
    revised = build_sleep_session_blob(reported=midnight + timedelta(hours=7), midnight=midnight, stages=[(1440, 1529, "deep")], score=80)
    other = build_sleep_session_blob(reported=midnight + timedelta(hours=8), midnight=midnight, stages=[(1700, 1719, "light")])
    sessions = sleep_from_sessions([first, revised, other], TZ, "gadgetbridge_zepp")
    assert [s.row["score"] for s in sessions] == [80, None]
    assert sessions[0].row["deep_s"] == 90 * 60 and sessions[0].row["awake_s"] == 0
    assert sessions[0].row["local_date"] == "2026-11-02"


def test_strap_daily_summary_uses_sleep_window_hrv(conn, config, tmp_path: Path):
    from wristpull.importers.phone import import_gadgetbridge_db

    db = build_gb_db(tmp_path / "Gadgetbridge.db")
    import_gadgetbridge_db(conn, config, db)
    from wristpull.daily import rebuild_daily_summary

    rebuild_daily_summary(conn, source="gadgetbridge_zepp")
    row = conn.execute(
        "SELECT sleep_duration_s, sleep_score, hrv_last_night_avg_ms FROM daily_summary WHERE source = 'gadgetbridge_zepp' AND local_date = '2026-11-01'"
    ).fetchone()
    assert row["sleep_duration_s"] == 90 * 60 and row["sleep_score"] == 70
    assert row["hrv_last_night_avg_ms"] == 45.0  # the one RMSSD sample lies inside the night


def test_is_accidental_workout():
    from wristpull.gadgetbridge import is_accidental_workout

    assert is_accidental_workout(180, None, None, None)
    assert not is_accidental_workout(180, None, None, 120)
    assert not is_accidental_workout(180, None, 500.0, None)
    assert not is_accidental_workout(900, None, None, None)
    assert not is_accidental_workout(None, 600, None, None)
