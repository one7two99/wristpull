"""Synthetic Zepp OS fetch buffers (the byte layouts the strap sends), no real data."""

from __future__ import annotations

import struct
from datetime import datetime

from gb_db import build_sleep_session_blob

__all__ = ["activity_minutes", "build_sleep_session_blob", "hrv_record", "respiration_record", "resting_hr_record", "spo2_record", "stress_minutes", "hr_max_record", "pai_record", "workout_summary", "workout_details"]


def activity_minutes(n: int, *, hr: int = 70, steps: int = 5, kind: int = 80, intensity: int = 20) -> bytes:
    """``n`` minutes of kind, intensity, steps, heart rate, ?, sleep, deep, rem."""
    return b"".join(bytes([kind, intensity, steps, hr, 15, 0, 0, 0]) for _ in range(n))


def stress_minutes(values: list[int | None]) -> bytes:
    return bytes(0xFF if v is None else v for v in values)


def _ts(dt: datetime) -> int:
    return int(dt.timestamp())


def spo2_record(ts: datetime, value: int, *, auto: bool = True) -> bytes:
    return struct.pack("<IB", _ts(ts), value | (0x80 if auto else 0)) + b"\0" * 60


def hrv_record(ts: datetime, value: int) -> bytes:
    return struct.pack("<IBB", _ts(ts), 0, value)


def resting_hr_record(ts: datetime, bpm: int, tz_quarters: int = 4) -> bytes:
    return struct.pack("<IbB", _ts(ts), tz_quarters, bpm)


def respiration_record(ts: datetime, rate: int, tz_quarters: int = 4) -> bytes:
    return struct.pack("<IbBBB", _ts(ts), tz_quarters, rate, 0, 1)


def hr_max_record(ts: datetime, bpm: int, tz_quarters: int = 4) -> bytes:
    return struct.pack("<IbB", _ts(ts), tz_quarters, bpm)


def pai_record(ts: datetime, *, today: float, total: float, kind: int = 5, tz_quarters: int = 4, low: float = 0.0, moderate: float = 0.0, high: float = 0.0) -> bytes:
    rec = bytes([kind]) + struct.pack("<Ib", _ts(ts), tz_quarters) + b"\0" * 31
    rec += struct.pack("<fffhhhff", low, moderate, high, 0, 0, 0, today, total) + b"\0" * 39
    assert len(rec) == 102
    return rec


# --- protobuf encoder (tests only) -------------------------------------------------------------------


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def pb_varint(field: int, value: int) -> bytes:
    return varint(field << 3) + varint(value)


def pb_float(field: int, value: float) -> bytes:
    return varint((field << 3) | 5) + struct.pack("<f", value)


def pb_bytes(field: int, payload: bytes) -> bytes:
    return varint((field << 3) | 2) + varint(len(payload)) + payload


def pb_packed(field: int, values: list[int]) -> bytes:
    return pb_bytes(field, b"".join(varint(v) for v in values))


def workout_summary(
    *,
    start: datetime,
    type_code: int = 0x01,
    auto: bool = False,
    total_s: int = 1800,
    workout_s: int | None = None,
    distance_m: float | None = 5000.0,
    calories: int | None = 320,
    hr: tuple[int, int, int] | None = (142, 171, 95),
    steps: tuple[float, float, int, int] | None = (2.8, 3.1, 95, 5100),  # avg cadence/s, max cadence/s, stride cm, steps
    pace: tuple[float, float] | None = (0.36, 0.3),  # s/m avg, best
    training: tuple[float, float, int, int] | None = (2.4, 0.5, 45, 48),  # aerobic, anaerobic, load, vo2max
    zones: tuple[list[int], list[int]] | None = ([84, 101, 118, 135, 152, 169], [0, 60, 600, 900, 240, 0]),
    elevation: tuple[int, int] | None = None,  # gain cm, loss cm
    version: int = 0x8000,
) -> bytes:
    """A Zepp OS workout summary: the version word, then the WorkoutSummary protobuf."""
    body = pb_bytes(1, b"2.1")
    body += pb_bytes(2, pb_varint(1, _ts(start)) + pb_varint(2, 0) + pb_varint(3, 8))
    body += pb_bytes(3, pb_varint(1, type_code) + pb_varint(2, 1 if auto else 0))
    if distance_m is not None:
        body += pb_bytes(4, pb_float(1, distance_m))
    body += pb_bytes(7, pb_varint(1, total_s) + pb_varint(2, total_s if workout_s is None else workout_s) + pb_varint(12, 180))
    if pace is not None:
        body += pb_bytes(10, pb_float(1, pace[0]) + pb_float(2, pace[1]))
    if steps is not None:
        body += pb_bytes(11, pb_float(1, steps[0]) + pb_float(2, steps[1]) + pb_varint(3, steps[2]) + pb_varint(4, steps[3]))
    if elevation is not None:
        body += pb_bytes(13, pb_varint(5, elevation[0]) + pb_varint(6, elevation[1]))
    if calories is not None:
        body += pb_bytes(16, pb_varint(1, calories))
    if hr is not None:
        body += pb_bytes(19, pb_varint(1, hr[0]) + pb_varint(2, hr[1]) + pb_varint(3, hr[2]))
    if training is not None:
        body += pb_bytes(21, pb_float(4, training[0]) + pb_float(5, training[1]) + pb_varint(6, training[2]) + pb_varint(7, training[3]))
    if zones is not None:
        body += pb_bytes(22, pb_varint(1, 1) + pb_packed(2, zones[0]) + pb_packed(3, zones[1]))
    return struct.pack("<H", version) + body


# --- BER-TLV details (tests only) --------------------------------------------------------------------


def tlv(tag: int, payload: bytes) -> bytes:
    head = bytes([tag]) if tag < 0x1F else bytes([0x1F, tag & 0xFF]) if tag == 0x1F09 else bytes([tag])
    if tag == 0x1F09:
        head = b"\x1f\x09"
    n = len(payload)
    length = bytes([n]) if n < 0x80 else b"\x81" + bytes([n]) if n < 256 else b"\x82" + n.to_bytes(2, "big")
    return head + length + payload


def d_timestamp(ms: int) -> bytes:
    return tlv(1, b"\0" * 4 + struct.pack("<q", ms))


def d_heart_rate(offset_ms: int, bpm: int) -> bytes:
    return tlv(8, struct.pack("<hB", offset_ms, bpm))


def d_speed(offset_ms: int, cadence: int, stride_cm: int, pace_s_km: int) -> bytes:
    return tlv(5, struct.pack("<hhhh", offset_ms, cadence, stride_cm, pace_s_km))


def d_distance(offset_ms: int, cm: int) -> bytes:
    return tlv(0x1F09, struct.pack("<hI", offset_ms, cm))


def d_status(offset_ms: int, code: int) -> bytes:
    return tlv(4, struct.pack("<hh", offset_ms, code))


def d_altitude(offset_ms: int, cm: int) -> bytes:
    return tlv(7, struct.pack("<hi", offset_ms, cm))


def d_gps(lon_raw: int, lat_raw: int) -> bytes:
    return tlv(2, b"\0" * 6 + struct.pack("<ii", lon_raw, lat_raw) + b"\0" * 6)


def d_gps_delta(offset_ms: int, dlon: int, dlat: int) -> bytes:
    return tlv(3, struct.pack("<hhhh", offset_ms, dlon, dlat, 2))


def d_lap(number: int, hr: int, pace_s_km: int, calories: int, distance_m: int, duration_ms: int) -> bytes:
    body = b"\0\0" + struct.pack("<H", number) + b"\x03" + bytes([hr]) + struct.pack("<HHH", pace_s_km, calories, distance_m) + b"\0" * 4 + struct.pack("<i", duration_ms)
    return tlv(11, body + b"\0" * (99 - len(body)))


def d_strength_set(reps: int, weight_dg: int) -> bytes:
    body = b"\0" * 15 + struct.pack("<H", reps) + b"\0" + struct.pack("<H", weight_dg) + b"\xff" * 14
    return tlv(15, body)


def workout_details(start_ms: int, seconds: int = 60, *, hr: int = 140, pace_s_km: int = 360) -> bytes:
    """A track with one point per second: heart rate, speed and cumulative distance, start and stop.

    Offsets are milliseconds since the last TIMESTAMP TLV and fit in an int16, so a new TIMESTAMP is
    written every 30 seconds, as the band does."""
    out = b""
    off = 0
    for i in range(seconds):
        if i % 30 == 0:
            out += d_timestamp(start_ms + i * 1000)
            if i == 0:
                out += d_status(0, 1)
        off = (i % 30) * 1000
        out += d_heart_rate(off, hr + i % 5) + d_speed(off, 170, 110, pace_s_km) + d_distance(off, int((i + 1) * 1000 / pace_s_km * 100))
    out += d_status(off, 6)
    return out
