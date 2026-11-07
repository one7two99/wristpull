"""Fetch kinds, the container file and the byte-to-row parsers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from zepp_buffers import activity_minutes, build_sleep_session_blob, hr_max_record, hrv_record, pai_record, respiration_record, resting_hr_record, spo2_record, stress_minutes, workout_details, workout_summary
from wristpull.zeppos.fetch import BY_NAME, KINDS, RAW_KINDS, WORKOUT_DETAILS, Buffer, buffer_end, pack_buffer, pack_workout, parse_buffer, split_workout, unpack_buffer

TZ = ZoneInfo("Europe/Berlin")
START = datetime(2026, 11, 6, 22, 0, tzinfo=UTC)  # 23:00 CET


def test_kind_table():
    assert [k.name for k in KINDS][:7] == ["activity", "stress", "spo2", "hrv", "resting_hr", "respiration", "sleep"]
    assert BY_NAME["stress"].code == 0x13 and BY_NAME["sleep"].record == 594
    assert "zepp_activity" in RAW_KINDS and BY_NAME["pai"].record == 102 and BY_NAME["workout"].code == 0x05


def test_container_roundtrip_and_names():
    buf = Buffer(BY_NAME["stress"], START, 4, stress_minutes([10, None, 30]))
    blob = pack_buffer(buf)
    assert blob[:4] == b"WPZB" and unpack_buffer(blob) == buf
    assert buf.file_name() == "stress_20261106T2200Z.bin" and buf.samples == 3
    with pytest.raises(ValueError):
        unpack_buffer(b"nope")
    with pytest.raises(ValueError):
        unpack_buffer(blob[:5] + b"\x09" + blob[6:])  # unknown kind code
    with pytest.raises(ValueError):
        unpack_buffer(blob[:4] + b"\x02" + blob[5:])  # unknown version


def test_activity_parser_counts_steps_per_local_day():
    # 120 minutes from 23:00 CET: the day changes after 60 minutes, the counter restarts
    data = activity_minutes(60, steps=5, hr=60) + activity_minutes(60, steps=10, hr=255)
    buf = Buffer(BY_NAME["activity"], START, 4, data)
    rows = parse_buffer(buf, TZ, "zepp_ble")
    assert len(rows.heart_rate) == 60 and rows.heart_rate[0]["bpm"] == 60  # 255 = no reading
    assert len(rows.steps) == 120 and "steps" in rows.replace_tables
    assert rows.steps[59]["steps"] == 300 and rows.steps[59]["local_date"] == "2026-11-06"
    assert rows.steps[60]["steps"] == 10 and rows.steps[119]["steps"] == 600 and rows.steps[60]["local_date"] == "2026-11-07"
    assert rows.steps[0]["ts_utc"] == "2026-11-06T22:00:00Z" and rows.steps[0]["tz"] == "Europe/Berlin"
    assert not rows.sleep  # nights come from the sleep-session kind only
    assert buffer_end(buf) == START + timedelta(minutes=119) and buf.samples == 120


def test_stress_parser_skips_gaps():
    rows = parse_buffer(Buffer(BY_NAME["stress"], START, 4, stress_minutes([None, 25, None, 40])), TZ, "zepp_ble")
    assert [(r["ts_utc"], r["level"]) for r in rows.stress] == [("2026-11-06T22:01:00Z", 25), ("2026-11-06T22:03:00Z", 40)]


def test_record_parsers():
    t1, t2 = START, START + timedelta(minutes=5)
    spo2 = parse_buffer(Buffer(BY_NAME["spo2"], t1, 4, b"\x02" + spo2_record(t1, 97) + spo2_record(t2, 95, auto=False) + spo2_record(t2 + timedelta(minutes=1), 0)), TZ, "zepp_ble")
    assert [(r["spo2_pct"], r["mode"]) for r in spo2.spo2] == [(97, "auto"), (95, "manual")]
    with pytest.raises(ValueError):
        parse_buffer(Buffer(BY_NAME["spo2"], t1, 4, b"\x01" + spo2_record(t1, 97)), TZ, "zepp_ble")
    hrv = parse_buffer(Buffer(BY_NAME["hrv"], t1, 4, hrv_record(t1, 45) + hrv_record(t2, 0)), TZ, "zepp_ble")
    assert [(r["ts_utc"], r["rmssd_ms"]) for r in hrv.hrv_values] == [("2026-11-06T22:00:00Z", 45.0)]
    resting = parse_buffer(Buffer(BY_NAME["resting_hr"], t1, 4, resting_hr_record(t1, 51) + resting_hr_record(t2, 255)), TZ, "zepp_ble")
    assert [r["resting_hr"] for r in resting.resting_hr] == [51]
    resp = parse_buffer(Buffer(BY_NAME["respiration"], t1, 4, respiration_record(t1, 14) + respiration_record(t2, 0)), TZ, "zepp_ble")
    assert [r["breaths_per_min"] for r in resp.respiration] == [14.0]
    buf = Buffer(BY_NAME["hrv"], t1, 4, hrv_record(t2, 45) + hrv_record(t1, 44))
    assert buffer_end(buf) == t2 and buf.samples == 2


def test_sleep_parser_uses_the_bands_sessions():
    midnight = datetime(2026, 11, 6, 23, 0, tzinfo=UTC)  # local midnight 2026-11-07
    stages = [(1380, 1409, "light"), (1410, 1439, "deep"), (1440, 1449, "light"), (1450, 1459, "rem")]
    blob = build_sleep_session_blob(reported=START + timedelta(hours=8), midnight=midnight, stages=stages, score=70, avg_hr=52)
    rows = parse_buffer(Buffer(BY_NAME["sleep"], START, 4, blob), TZ, "zepp_ble")
    assert len(rows.sleep) == 1
    night = rows.sleep[0].row
    assert night["start_utc"] == "2026-11-06T22:00:00Z" and night["deep_s"] == 1800 and night["score"] == 70 and night["source"] == "zepp_ble"
    assert len(rows.sleep[0].stages) == 4
    opaque = parse_buffer(Buffer(WORKOUT_DETAILS, START, 4, b"\x05" + b"\0" * 40), TZ, "zepp_ble")
    assert opaque.counts() == {}


def test_pai_and_hr_max_parsers():
    t1, t2 = START, START + timedelta(hours=1)
    pai = parse_buffer(Buffer(BY_NAME["pai"], t1, 4, pai_record(t1, today=3.5, total=41.25) + pai_record(t2, today=0, total=0, kind=0)), TZ, "zepp_ble")
    assert [(m["metric"], m["value"], m["ts_utc"]) for m in pai.metrics] == [("pai_total", 41.25, "2026-11-06T22:00:00Z"), ("pai_today", 3.5, "2026-11-06T22:00:00Z")]
    buf = Buffer(BY_NAME["pai"], t1, 4, pai_record(t1, today=1, total=2) + pai_record(t2, today=1, total=2))
    assert buffer_end(buf) == t2 and buf.samples == 2
    hr = parse_buffer(Buffer(BY_NAME["hr_max"], t1, 4, hr_max_record(t1, 148) + hr_max_record(t2, 255)), TZ, "zepp_ble")
    assert [(m["metric"], m["value"]) for m in hr.metrics] == [("hr_max", 148.0)]


def test_workout_buffer_roundtrip_and_rows():
    start = datetime(2026, 11, 6, 17, 30, tzinfo=UTC)
    summary = workout_summary(start=start, type_code=0x01, total_s=1800, distance_m=5000.0)
    details = workout_details(int(start.timestamp() * 1000), seconds=30)
    data = pack_workout(summary, details)
    assert split_workout(data) == (summary, details)
    buf = Buffer(BY_NAME["workout"], start, 4, data)
    assert buffer_end(buf) == start + timedelta(seconds=1800) and buf.samples == 1
    rows = parse_buffer(buf, TZ, "zepp_ble")
    assert len(rows.activities) == 1
    act = rows.activities[0]
    assert act.row["sport"] == "running" and act.row["start_utc"] == "2026-11-06T17:30:00Z" and act.row["end_utc"] == "2026-11-06T18:00:00Z"
    assert act.row["distance_m"] == 5000.0 and act.row["avg_hr"] == 142 and act.row["max_hr"] == 171 and act.row["calories"] == 320
    assert act.row["hr_zone_times_s"] == "[0, 60, 600, 900, 240, 0]" and act.row["aerobic_te"] == pytest.approx(2.4, abs=0.01)
    assert len(act.records) == 30 and act.records[0]["ts_utc"] == "2026-11-06T17:30:00Z" and act.records[0]["hr"] == 140 and act.records[1]["hr"] == 141
    assert act.records[29]["distance_m"] == pytest.approx(30 * 1000 / 360, abs=0.02) and act.records[0]["speed_mps"] == pytest.approx(1000 / 360)
    assert [(m["metric"], m["value"]) for m in rows.metrics] == [("vo2_max", 48.0)]
    with pytest.raises(ValueError):
        split_workout(b"\x09\0\0\0ab")
    with pytest.raises(ValueError):
        parse_buffer(Buffer(BY_NAME["workout"], start, 4, pack_workout(b"\x01\x00" + summary[2:], b"")), TZ, "zepp_ble")
