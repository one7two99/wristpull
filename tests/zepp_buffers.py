"""Synthetic Zepp OS fetch buffers (the byte layouts the strap sends), no real data."""

from __future__ import annotations

import struct
from datetime import datetime

from gb_db import build_sleep_session_blob

__all__ = ["activity_minutes", "build_sleep_session_blob", "hrv_record", "respiration_record", "resting_hr_record", "spo2_record", "stress_minutes"]


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
