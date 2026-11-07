"""Protobuf decoder, the workout summary and the BER-TLV details track."""

from __future__ import annotations

import struct
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from zepp_buffers import d_altitude, d_distance, d_gps, d_gps_delta, d_heart_rate, d_lap, d_speed, d_status, d_strength_set, d_timestamp, pb_bytes, pb_float, pb_packed, pb_varint, tlv, workout_details, workout_summary
from wristpull.rows import ParsedRows
from wristpull.zeppos import protobuf as pb
from wristpull.zeppos.workout import AUTO_DETECTED_CODE, parse_details, parse_summary, sport_name, workout_rows

TZ = ZoneInfo("Europe/Berlin")
START = datetime(2026, 11, 6, 17, 30, tzinfo=UTC)


def test_protobuf_decoder_basics():
    msg = pb_bytes(1, b"2.1") + pb_varint(2, 300) + pb_float(3, 1.5) + pb_packed(4, [1, 200, 3]) + bytes([0x29]) + struct.pack("<d", 2.5)
    f = pb.decode(msg)
    assert pb.as_str(pb.first(f, 1)) == "2.1" and pb.as_int(pb.first(f, 2)) == 300 and pb.as_float(pb.first(f, 3)) == 1.5
    assert pb.packed_ints(pb.first(f, 4)) == [1, 200, 3] and pb.as_float(pb.first(f, 5)) == 2.5
    assert pb.first(f, 9) is None and pb.packed_ints(None) == [] and pb.packed_ints(7) == [7] and pb.as_int(None) is None
    assert pb.as_int((1 << 32) - 5) == -5  # int32 written as negative
    with pytest.raises(pb.ProtobufError):
        pb.decode(b"\x08")  # truncated varint
    with pytest.raises(pb.ProtobufError):
        pb.decode(b"\x0a\x05ab")  # truncated bytes
    with pytest.raises(pb.ProtobufError):
        pb.decode(bytes([0x0F]))  # wire type 7
    with pytest.raises(pb.ProtobufError):
        pb.submessage({1: [5]}, 1)


def test_parse_summary_fields():
    s = parse_summary(workout_summary(start=START, type_code=0x04, total_s=3600, workout_s=3500, distance_m=24000.0, elevation=(12345, 6789)))
    assert s.version == "2.1" and s.sport == "cycling" and not s.auto and s.start_utc == START
    assert (s.total_duration_s, s.workout_duration_s, s.distance_m, s.calories) == (3600, 3500, 24000.0, 320)
    assert (s.hr_avg, s.hr_max, s.hr_min) == (142, 171, 95) and s.steps == 5100 and s.stride_cm == 95
    assert s.cadence_avg == pytest.approx(2.8 * 60) and s.cadence_max == pytest.approx(3.1 * 60)
    assert s.pace_avg == pytest.approx(0.36) and s.pace_best == pytest.approx(0.3)
    assert s.elevation_gain_m == 123.45 and s.elevation_loss_m == 67.89
    assert s.aerobic_te == pytest.approx(2.4) and s.workout_load == 45 and s.vo2_max == 48
    assert s.hr_zone_bounds == [84, 101, 118, 135, 152, 169] and s.hr_zone_times_s == [0, 60, 600, 900, 240, 0]
    assert s.end_utc(START).isoformat() == "2026-11-06T18:30:00+00:00"
    auto = parse_summary(workout_summary(start=START, type_code=AUTO_DETECTED_CODE, auto=True, distance_m=None, pace=None, steps=None))
    assert auto.sport == "auto_detected" and auto.auto and auto.distance_m is None
    assert sport_name(0x99) == "dodgeball" and sport_name(0xEE) == "zepp_0xee" and sport_name(None) is None
    with pytest.raises(ValueError):
        parse_summary(workout_summary(start=START, version=0x0102))
    with pytest.raises(ValueError):
        parse_summary(b"\x00")


def test_parse_details_track():
    start_ms = int(START.timestamp() * 1000)
    data = d_timestamp(start_ms) + d_status(0, 1) + d_gps(360_000_000, 150_000_000) + d_heart_rate(0, 120) + d_speed(0, 160, 100, 400) + d_distance(0, 250)
    data += d_heart_rate(1000, 125) + d_gps_delta(1000, 300, -150) + d_altitude(1000, 12345) + d_distance(1000, 500)
    data += tlv(0x77, b"\x01\x02") + tlv(8, b"\x00\x00")  # unknown tag, wrong length: both skipped
    data += d_lap(1, 130, 380, 40, 1000, 240000) + d_strength_set(12, 200) + d_strength_set(8, 0xFFFF)
    data += d_status(1000, 6)
    d = parse_details(data)
    assert [(p.time_ms - start_ms, p.hr, p.distance_m, p.cadence) for p in d.points] == [(0, 120, 2.5, 160), (1000, 125, 5.0, 160)]
    assert d.points[0].speed_mps == pytest.approx(2.5) and d.points[0].lat == pytest.approx(50.0) and d.points[0].lon == pytest.approx(120.0)
    assert d.points[1].lat == pytest.approx(49.99995) and d.points[1].altitude_m == 123.45
    assert d.laps[0].number == 1 and d.laps[0].duration_ms == 240000 and d.laps[0].distance_m == 1000
    assert [(s.reps, s.weight_kg) for s in d.sets] == [(12, 20.0), (8, None)]
    assert [name for _, name in d.statuses] == ["start", "stop"] and d.unknown_tags == {0x77: 1, 8: 1}
    with pytest.raises(ValueError):
        parse_details(b"\x08\x05\x01")  # truncated
    long_len = parse_details(tlv(8, b"\x00\x00\x90") + b"\x1f\x09\x81\x06" + struct.pack("<hI", 0, 100))
    assert long_len.points == []  # no timestamp yet: nothing is flushed, but parsing succeeds
    # a new TIMESTAMP resets the offset: one point per second over the 30-second boundary
    track = parse_details(workout_details(start_ms, seconds=35))
    assert [p.time_ms - start_ms for p in track.points] == [i * 1000 for i in range(35)]


def test_workout_rows_and_accidental_start():
    out = ParsedRows()
    summary = parse_summary(workout_summary(start=START, type_code=0x34, total_s=120, distance_m=None, hr=None, pace=None, steps=None, training=None, zones=None))
    assert workout_rows(summary, None, START, TZ, "zepp_ble", "S1", out) is None and out.activities == []
    summary = parse_summary(workout_summary(start=START, type_code=0x34, total_s=120, distance_m=None, pace=None, steps=None, training=None, zones=None))
    act = workout_rows(summary, parse_details(workout_details(int(START.timestamp() * 1000), seconds=5)), START, TZ, "zepp_ble", "S1", out)
    assert act is not None and act.row["sport"] == "strength_training" and act.row["device_serial"] == "S1" and act.row["avg_speed_mps"] is None
    assert len(act.records) == 5 and out.metrics == [] and act.row["local_date"] == "2026-11-06"
