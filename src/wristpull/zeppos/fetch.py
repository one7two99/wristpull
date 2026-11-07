"""Fetch kinds, the archive container for fetched buffers, and the parsers from bytes to rows.

Every buffer the band sends is archived as a small container file (``WPZB`` header with the kind,
the UTC start and the band's offset, then the bytes exactly as received) so a parser change can be
replayed with ``wristpull reparse --kind zepp_<name>``. The record layouts are the ones Gadgetbridge
documents in its ``Fetch*Operation`` classes and were verified against the strap on 2026-11-07.
"""

from __future__ import annotations

import struct
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from ..gadgetbridge import HUAMI_HR_INVALID, sleep_from_sessions, stamp
from ..rows import ParsedRows
from ..timeutil import local_date
from .workout import parse_details, parse_summary, workout_rows

MAGIC = b"WPZB"
CONTAINER_VERSION = 1
HEADER = struct.Struct("<4sBBIbx")  # magic, version, kind code, start_utc, tz quarters, pad
MINUTE = timedelta(minutes=1)
SPO2_VERSION = 2
SPO2_RECORD = 65
PAI_RECORD = 102
PAI_TYPE_CURRENT = 5
WORKOUT_DETAILS_CODE = 0x06
WORKOUT_HEADER = struct.Struct("<I")  # summary length, then summary, then details


@dataclass(frozen=True)
class Kind:
    name: str
    code: int
    minutes: bool  # one sample per minute from the start time (no timestamps in the records)
    record: int  # bytes per sample/record; 0 = opaque
    parsed: bool = True  # False: archived for a later parser, no rows today
    ts_offset: int = 0  # where the u32 timestamp sits inside a record

    @property
    def raw_kind(self) -> str:
        return f"zepp_{self.name}"

    @property
    def opaque(self) -> bool:
        return self.record == 0


KINDS: tuple[Kind, ...] = (
    Kind("activity", 0x01, True, 8),
    Kind("stress", 0x13, True, 1),
    Kind("spo2", 0x25, False, SPO2_RECORD),
    Kind("hrv", 0x49, False, 6),
    Kind("resting_hr", 0x3A, False, 6),
    Kind("respiration", 0x38, False, 8),
    Kind("sleep", 0x48, False, 594),
    Kind("hr_max", 0x3D, False, 6),
    Kind("pai", 0x0D, False, PAI_RECORD, ts_offset=1),
    Kind("workout", 0x05, False, 0),  # one summary per fetch, its details fetched with code 0x06
)
WORKOUT_DETAILS = Kind("workout_details", WORKOUT_DETAILS_CODE, False, 0, parsed=False)
BY_NAME = {k.name: k for k in KINDS}
BY_CODE = {k.code: k for k in KINDS}
BY_RAW_KIND = {k.raw_kind: k for k in KINDS}
RAW_KINDS = tuple(k.raw_kind for k in KINDS)


@dataclass(frozen=True)
class Buffer:
    kind: Kind
    start_utc: datetime
    tz_quarters: int
    data: bytes

    @property
    def samples(self) -> int:
        if self.kind.name == "workout":
            return 1
        if self.kind.minutes:
            return len(self.data) // self.kind.record
        if self.kind.record:
            body = len(self.data) - (1 if self.kind.name == "spo2" else 0)
            return body // self.kind.record
        return len(self.data)  # opaque kinds: bytes

    def file_name(self) -> str:
        return f"{self.kind.name}_{self.start_utc.strftime('%Y%m%dT%H%MZ')}.bin"


def pack_buffer(buffer: Buffer) -> bytes:
    return HEADER.pack(MAGIC, CONTAINER_VERSION, buffer.kind.code, int(buffer.start_utc.timestamp()), buffer.tz_quarters) + buffer.data


def unpack_buffer(blob: bytes) -> Buffer:
    if len(blob) < HEADER.size or blob[:4] != MAGIC:
        raise ValueError("not a wristpull Zepp buffer file")
    magic, version, code, start, tz = HEADER.unpack_from(blob)
    if version != CONTAINER_VERSION:
        raise ValueError(f"unsupported Zepp buffer version {version}")
    kind = BY_CODE.get(code)
    if kind is None:
        raise ValueError(f"unknown Zepp fetch kind 0x{code:02x}")
    return Buffer(kind, datetime.fromtimestamp(start, UTC), tz, bytes(blob[HEADER.size :]))


def pack_workout(summary: bytes, details: bytes) -> bytes:
    return WORKOUT_HEADER.pack(len(summary)) + summary + details


def split_workout(data: bytes) -> tuple[bytes, bytes]:
    if len(data) < WORKOUT_HEADER.size:
        raise ValueError("workout buffer too short")
    (n,) = WORKOUT_HEADER.unpack_from(data)
    if WORKOUT_HEADER.size + n > len(data):
        raise ValueError("workout buffer: summary length exceeds the data")
    return data[WORKOUT_HEADER.size : WORKOUT_HEADER.size + n], data[WORKOUT_HEADER.size + n :]


def buffer_end(buffer: Buffer) -> datetime:
    """UTC time of the last sample in the buffer (what the next fetch continues from)."""
    kind = buffer.kind
    if kind.name == "workout":
        summary = parse_summary(split_workout(buffer.data)[0])
        return summary.end_utc(summary.start_utc or buffer.start_utc)
    if kind.minutes:
        n = len(buffer.data) // kind.record
        return buffer.start_utc + MINUTE * max(n - 1, 0)
    last = buffer.start_utc
    for ts in _record_timestamps(buffer):
        last = max(last, ts)
    return last


def _record_timestamps(buffer: Buffer):
    kind = buffer.kind
    offset = 1 if kind.name == "spo2" else 0
    if not kind.record:
        return
    for pos in range(offset, len(buffer.data) - kind.record + 1, kind.record):
        (ts,) = struct.unpack_from("<I", buffer.data, pos + kind.ts_offset)
        yield datetime.fromtimestamp(ts, UTC)


def _records(buffer: Buffer):
    kind = buffer.kind
    offset = 1 if kind.name == "spo2" else 0
    for pos in range(offset, len(buffer.data) - kind.record + 1, kind.record):
        yield buffer.data[pos : pos + kind.record]


def parse_buffer(buffer: Buffer, tz: ZoneInfo, source: str) -> ParsedRows:
    """Rows for one archived buffer; opaque kinds give an empty result."""
    out = ParsedRows()
    kind = buffer.kind
    if not kind.parsed:
        return out
    parser = _PARSERS[kind.name]
    parser(buffer, tz, source, out)
    return out


def _parse_activity(buffer: Buffer, tz: ZoneInfo, source: str, out: ParsedRows) -> None:
    """8 bytes per minute: kind, intensity, steps, heart rate, ?, sleep, deep, rem.

    Steps become the cumulative count per local day (what the ``steps`` table holds), so a buffer
    must start at a local midnight or at the beginning of the band's data; the importer sees to that.
    Nights are not derived from the sleep flags: the band's own sleep sessions carry the stages.
    """
    daily: dict[str, int] = defaultdict(int)
    for i, rec in enumerate(_records(buffer)):
        ts = buffer.start_utc + MINUTE * i
        hr = rec[3]
        if hr not in HUAMI_HR_INVALID and 25 <= hr <= 250:
            out.heart_rate.append({"source": source, **stamp(ts, tz), "bpm": int(hr)})
        steps = rec[2]
        if steps == 0xFF:
            continue
        day = local_date(ts, tz)
        daily[day] += int(steps)
        out.steps.append(
            {"source": source, **stamp(ts, tz), "activity_type": "all", "steps": daily[day], "distance_m": None, "active_time_s": None, "active_calories": None}
        )
    if out.steps:
        out.replace_tables.add("steps")


def _parse_stress(buffer: Buffer, tz: ZoneInfo, source: str, out: ParsedRows) -> None:
    for i, level in enumerate(buffer.data):
        if level == 0xFF:
            continue
        out.stress.append({"source": source, **stamp(buffer.start_utc + MINUTE * i, tz), "level": int(level)})


def _parse_spo2(buffer: Buffer, tz: ZoneInfo, source: str, out: ParsedRows) -> None:
    if not buffer.data or buffer.data[0] != SPO2_VERSION:
        raise ValueError(f"unknown SpO2 buffer version {buffer.data[:1].hex() or 'empty'}")
    for rec in _records(buffer):
        ts, raw = struct.unpack_from("<IB", rec)
        value = raw & 0x7F
        if value <= 0:
            continue
        out.spo2.append({"source": source, **stamp(datetime.fromtimestamp(ts, UTC), tz), "spo2_pct": value, "confidence": None, "mode": "auto" if raw & 0x80 else "manual"})


def _parse_hrv(buffer: Buffer, tz: ZoneInfo, source: str, out: ParsedRows) -> None:
    for rec in _records(buffer):
        ts, _unknown, value = struct.unpack_from("<IBB", rec)
        if value > 0:
            out.hrv_values.append({"source": source, **stamp(datetime.fromtimestamp(ts, UTC), tz), "rmssd_ms": float(value)})


def _parse_resting_hr(buffer: Buffer, tz: ZoneInfo, source: str, out: ParsedRows) -> None:
    for rec in _records(buffer):
        ts, _tz, bpm = struct.unpack_from("<IbB", rec)
        if bpm not in HUAMI_HR_INVALID:
            out.resting_hr.append({"source": source, **stamp(datetime.fromtimestamp(ts, UTC), tz), "resting_hr": int(bpm), "current_day_resting_hr": None})


def _parse_respiration(buffer: Buffer, tz: ZoneInfo, source: str, out: ParsedRows) -> None:
    for rec in _records(buffer):
        ts, _tz, rate = struct.unpack_from("<IbB", rec)
        if rate > 0:
            out.respiration.append({"source": source, **stamp(datetime.fromtimestamp(ts, UTC), tz), "breaths_per_min": float(rate)})


def _parse_sleep(buffer: Buffer, tz: ZoneInfo, source: str, out: ParsedRows) -> None:
    out.sleep.extend(sleep_from_sessions(list(_records(buffer)), tz, source))


def _parse_hr_max(buffer: Buffer, tz: ZoneInfo, source: str, out: ParsedRows) -> None:
    """6 bytes: ts, tz, bpm - the day's maximum heart rate as the band reports it."""
    for rec in _records(buffer):
        ts, _tz, bpm = struct.unpack_from("<IbB", rec)
        if bpm not in HUAMI_HR_INVALID:
            out.metrics.append({"source": source, **stamp(datetime.fromtimestamp(ts, UTC), tz), "metric": "hr_max", "value": float(bpm), "sport": None})


def _parse_pai(buffer: Buffer, tz: ZoneInfo, source: str, out: ParsedRows) -> None:
    """102 bytes: type (5 = current), ts, tz, 31 ?, pai low/moderate/high f32, minutes low/moderate/high i16,
    pai today f32, pai total f32, 39 ?. Type 0 records (before a factory reset) are skipped like Gadgetbridge does."""
    for rec in _records(buffer):
        kind_byte = rec[0]
        if kind_byte != PAI_TYPE_CURRENT:
            continue
        ts = struct.unpack_from("<I", rec, 1)[0]
        pai_today, pai_total = struct.unpack_from("<ff", rec, 55)
        when = stamp(datetime.fromtimestamp(ts, UTC), tz)
        out.metrics.append({"source": source, **when, "metric": "pai_total", "value": round(float(pai_total), 2), "sport": None})
        out.metrics.append({"source": source, **when, "metric": "pai_today", "value": round(float(pai_today), 2), "sport": None})


def _parse_workout(buffer: Buffer, tz: ZoneInfo, source: str, out: ParsedRows) -> None:
    summary_bytes, details_bytes = split_workout(buffer.data)
    summary = parse_summary(summary_bytes)
    details = parse_details(details_bytes) if details_bytes else None
    workout_rows(summary, details, buffer.start_utc, tz, source, None, out)


_PARSERS = {
    "activity": _parse_activity,
    "stress": _parse_stress,
    "spo2": _parse_spo2,
    "hrv": _parse_hrv,
    "resting_hr": _parse_resting_hr,
    "respiration": _parse_respiration,
    "sleep": _parse_sleep,
    "hr_max": _parse_hr_max,
    "pai": _parse_pai,
    "workout": _parse_workout,
}
