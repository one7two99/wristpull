"""Parse an exported Gadgetbridge SQLite database into wristpull rows.

Only non-Garmin devices are read from the database: Garmin data arrives as the
watch's own FIT files, which Gadgetbridge keeps on the phone and which wristpull
parses with the same code as the USB import. Zepp OS devices (Amazfit Helio
Strap) have no raw files, their samples live in the HUAMI_* and GENERIC_* tables.

Timestamp units differ per table: *_ACTIVITY_SAMPLE tables are in seconds, the
other sample tables in milliseconds since the Unix epoch.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .rows import ParsedActivity, ParsedRows, ParsedSleep
from .timeutil import iso_utc, local_date

SOURCE_ZEPP = "gadgetbridge_zepp"
SOURCE_GARMIN = "gadgetbridge_garmin"
HUAMI_HR_INVALID = {0, 255, -1, None}
SLEEP_GAP = timedelta(minutes=30)
SLEEP_MIN_MINUTES = 20

# Gadgetbridge ActivityKind codes that matter for sport names (subset; unknown codes become kind_<n>)
ACTIVITY_KINDS = {
    1: "generic",
    16: "running",
    32: "cycling",
    64: "walking",
    128: "swimming",
    256: "treadmill",
    512: "hiking",
    1024: "elliptical",
    2048: "strength_training",
    4096: "yoga",
    8192: "indoor_cycling",
    16384: "soccer",
    32768: "rowing",
    65536: "jump_rope",
    131072: "pool_swimming",
    262144: "outdoor_swimming",
}


@dataclass(frozen=True)
class GbDevice:
    id: int
    name: str
    manufacturer: str | None
    mac: str
    type_name: str
    model: str | None

    @property
    def source(self) -> str | None:
        t = self.type_name.upper()
        if t.startswith("GARMIN"):
            return SOURCE_GARMIN
        if any(k in t for k in ("AMAZFIT", "HUAMI", "ZEPP", "MIBAND", "XIAOMI")):
            return SOURCE_ZEPP
        return None


def open_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def devices(conn: sqlite3.Connection) -> list[GbDevice]:
    out = []
    for r in conn.execute("SELECT _id, NAME, MANUFACTURER, IDENTIFIER, TYPE_NAME, MODEL FROM DEVICE ORDER BY _id"):
        out.append(GbDevice(int(r[0]), r[1] or "", r[2], r[3] or "", r[4] or "", r[5]))
    return out


def _ts_ms(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value) / 1000, UTC)


def _ts_s(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value), UTC)


def _stamp(dt: datetime, tz: ZoneInfo, *, shift: int = 0) -> dict[str, str]:
    return {"ts_utc": iso_utc(dt), "tz": tz.key, "local_date": local_date(dt, tz, shift_seconds=shift)}


def _rows(conn: sqlite3.Connection, table: str, device_id: int, order: str = "TIMESTAMP") -> list[sqlite3.Row]:
    if table not in tables(conn):
        return []
    return conn.execute(f'SELECT * FROM "{table}" WHERE DEVICE_ID = ? ORDER BY {order}', (device_id,)).fetchall()


def _sleep_from_minutes(minutes: list[tuple[datetime, str]], tz: ZoneInfo, source: str) -> list[ParsedSleep]:
    """Build sleep sessions from per-minute stage flags (light/deep/rem)."""
    sessions: list[list[tuple[datetime, str]]] = []
    for item in minutes:
        if sessions and item[0] - sessions[-1][-1][0] <= SLEEP_GAP:
            sessions[-1].append(item)
        else:
            sessions.append([item])
    out: list[ParsedSleep] = []
    for chunk in sessions:
        if len(chunk) < SLEEP_MIN_MINUTES:
            continue
        stages: list[dict[str, Any]] = []
        durations: dict[str, int] = defaultdict(int)
        cur_stage, cur_start = chunk[0][1], chunk[0][0]
        prev = chunk[0][0]
        for ts, stage in chunk[1:] + [(chunk[-1][0] + timedelta(minutes=1), None)]:
            gap = ts - prev
            if gap > timedelta(minutes=1) and stage is not None:
                # unflagged minutes inside a session count as awake
                stages.append({"start_utc": iso_utc(cur_start), "end_utc": iso_utc(prev + timedelta(minutes=1)), "stage": cur_stage})
                durations[cur_stage] += int((prev + timedelta(minutes=1) - cur_start).total_seconds())
                stages.append({"start_utc": iso_utc(prev + timedelta(minutes=1)), "end_utc": iso_utc(ts), "stage": "awake"})
                durations["awake"] += int((ts - prev - timedelta(minutes=1)).total_seconds())
                cur_stage, cur_start = stage, ts
            elif stage != cur_stage:
                stages.append({"start_utc": iso_utc(cur_start), "end_utc": iso_utc(ts), "stage": cur_stage})
                durations[cur_stage] += int((ts - cur_start).total_seconds())
                cur_stage, cur_start = stage, ts
            prev = ts
        start, end = chunk[0][0], chunk[-1][0] + timedelta(minutes=1)
        out.append(
            ParsedSleep(
                row={
                    "source": source,
                    "start_utc": iso_utc(start),
                    "end_utc": iso_utc(end),
                    "tz": tz.key,
                    "local_date": local_date(end, tz),
                    "duration_s": int((end - start).total_seconds()) - durations.get("awake", 0),
                    "deep_s": durations.get("deep", 0),
                    "light_s": durations.get("light", 0),
                    "rem_s": durations.get("rem", 0),
                    "awake_s": durations.get("awake", 0),
                    "unmeasurable_s": 0,
                    "score": None,
                    "quality_score": None,
                    "recovery_score": None,
                    "awakenings": sum(1 for s in stages if s["stage"] == "awake"),
                    "avg_stress": None,
                },
                stages=stages,
            )
        )
    return out


def parse_zepp_device(conn: sqlite3.Connection, device: GbDevice, tz: ZoneInfo, out: ParsedRows) -> None:
    src = SOURCE_ZEPP
    did = device.id

    # per-minute activity samples: heart rate, steps (converted to cumulative per local day), sleep flags
    daily_steps: dict[str, int] = defaultdict(int)
    sleep_minutes: list[tuple[datetime, str]] = []
    for r in _rows(conn, "HUAMI_EXTENDED_ACTIVITY_SAMPLE", did):
        ts = _ts_s(r["TIMESTAMP"])
        if ts is None:
            continue
        hr = r["HEART_RATE"]
        if hr not in HUAMI_HR_INVALID and 25 <= int(hr) <= 250:
            out.heart_rate.append({"source": src, **_stamp(ts, tz), "bpm": int(hr)})
        steps = r["STEPS"]
        if steps is not None and int(steps) >= 0:
            day = local_date(ts, tz)
            daily_steps[day] += int(steps)
            out.steps.append(
                {
                    "source": src,
                    **_stamp(ts, tz),
                    "activity_type": "all",
                    "steps": daily_steps[day],
                    "distance_m": None,
                    "active_time_s": None,
                    "active_calories": None,
                }
            )
        if r["SLEEP"]:
            stage = "deep" if r["DEEP_SLEEP"] else ("rem" if r["REM_SLEEP"] else "light")
            sleep_minutes.append((ts, stage))
    if out.steps:
        out.replace_tables.add("steps")
    out.sleep.extend(_sleep_from_minutes(sleep_minutes, tz, src))

    for r in _rows(conn, "HUAMI_STRESS_SAMPLE", did):
        ts = _ts_ms(r["TIMESTAMP"])
        if ts is not None and r["STRESS"] is not None and int(r["STRESS"]) >= 0:
            out.stress.append({"source": src, **_stamp(ts, tz), "level": int(r["STRESS"])})
    for r in _rows(conn, "HUAMI_SPO2_SAMPLE", did):
        ts = _ts_ms(r["TIMESTAMP"])
        if ts is not None and r["SPO2"] and int(r["SPO2"]) > 0:
            out.spo2.append({"source": src, **_stamp(ts, tz), "spo2_pct": int(r["SPO2"]), "confidence": None, "mode": str(r["TYPE_NUM"]) if r["TYPE_NUM"] is not None else None})
    for r in _rows(conn, "HUAMI_HEART_RATE_RESTING_SAMPLE", did):
        ts = _ts_ms(r["TIMESTAMP"])
        if ts is not None and r["HEART_RATE"] not in HUAMI_HR_INVALID:
            out.resting_hr.append({"source": src, **_stamp(ts, tz), "resting_hr": int(r["HEART_RATE"]), "current_day_resting_hr": None})
    for r in _rows(conn, "HUAMI_SLEEP_RESPIRATORY_RATE_SAMPLE", did):
        ts = _ts_ms(r["TIMESTAMP"])
        if ts is not None and r["RATE"] and int(r["RATE"]) > 0:
            out.respiration.append({"source": src, **_stamp(ts, tz), "breaths_per_min": float(r["RATE"])})
    for r in _rows(conn, "GENERIC_RESPIRATORY_RATE_SAMPLE", did):
        ts = _ts_ms(r["TIMESTAMP"])
        if ts is not None and r["RESPIRATORY_RATE"] and float(r["RESPIRATORY_RATE"]) > 0:
            out.respiration.append({"source": src, **_stamp(ts, tz), "breaths_per_min": float(r["RESPIRATORY_RATE"])})
    for r in _rows(conn, "GENERIC_HRV_VALUE_SAMPLE", did):
        ts = _ts_ms(r["TIMESTAMP"])
        if ts is not None and r["VALUE"] and int(r["VALUE"]) > 0:
            out.hrv_values.append({"source": src, **_stamp(ts, tz), "rmssd_ms": float(r["VALUE"])})
    for r in _rows(conn, "GENERIC_TEMPERATURE_SAMPLE", did):
        ts = _ts_ms(r["TIMESTAMP"])
        if ts is not None and r["TEMPERATURE"] is not None:
            out.temperature.append({"source": src, **_stamp(ts, tz), "celsius": round(float(r["TEMPERATURE"]), 2), "kind": "skin"})
    for r in _rows(conn, "GENERIC_STRESS_SAMPLE", did):
        ts = _ts_ms(r["TIMESTAMP"])
        if ts is not None and r["STRESS"] is not None and int(r["STRESS"]) >= 0:
            out.stress.append({"source": src, **_stamp(ts, tz), "level": int(r["STRESS"])})
    for r in _rows(conn, "GENERIC_SPO2_SAMPLE", did):
        ts = _ts_ms(r["TIMESTAMP"])
        if ts is not None and r["SPO2"] and int(r["SPO2"]) > 0:
            out.spo2.append({"source": src, **_stamp(ts, tz), "spo2_pct": int(r["SPO2"]), "confidence": None, "mode": None})
    for r in _rows(conn, "GENERIC_BODY_ENERGY_SAMPLE", did):
        ts = _ts_ms(r["TIMESTAMP"])
        if ts is not None and r["ENERGY"] is not None:
            out.body_battery.append({"source": src, **_stamp(ts, tz), "level": int(r["ENERGY"])})
    for r in _rows(conn, "HUAMI_PAI_SAMPLE", did):
        ts = _ts_ms(r["TIMESTAMP"])
        if ts is not None and r["PAI_TOTAL"] is not None:
            out.metrics.append({"source": src, **_stamp(ts, tz), "metric": "pai_total", "value": float(r["PAI_TOTAL"]), "sport": None})
            if r["PAI_TODAY"] is not None:
                out.metrics.append({"source": src, **_stamp(ts, tz), "metric": "pai_today", "value": float(r["PAI_TODAY"]), "sport": None})

    out.activities.extend(_workouts(conn, device, tz, src))


def _json(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _summary_value(summary: dict[str, Any], *keys: str) -> float | None:
    """Gadgetbridge summary JSON: {"key": {"value": 12.3, "unit": "..."}} or plain numbers."""
    for key in keys:
        entry = summary.get(key)
        if isinstance(entry, dict):
            entry = entry.get("value")
        if isinstance(entry, (int, float)):
            return float(entry)
        if isinstance(entry, str):
            try:
                return float(entry)
            except ValueError:
                continue
    return None


def _workouts(conn: sqlite3.Connection, device: GbDevice, tz: ZoneInfo, src: str) -> list[ParsedActivity]:
    out: list[ParsedActivity] = []
    for r in _rows(conn, "BASE_ACTIVITY_SUMMARY", device.id, order="START_TIME"):
        start, end = _ts_ms(r["START_TIME"]), _ts_ms(r["END_TIME"])
        if start is None:
            continue
        kind = r["OVERRIDE_ACTIVITY_KIND"] or r["ACTIVITY_KIND"]
        summary = _json(r["SUMMARY_DATA"])
        avg_hr = _summary_value(summary, "averageHR", "avgHeartRate")
        max_hr = _summary_value(summary, "maxHR", "maxHeartRate")
        timer = _summary_value(summary, "activeSeconds", "activeTime")
        out.append(
            ParsedActivity(
                row={
                    "source": src,
                    "start_utc": iso_utc(start),
                    "end_utc": iso_utc(end) if end else None,
                    "tz": tz.key,
                    "local_date": local_date(start, tz),
                    "sport": ACTIVITY_KINDS.get(int(kind), f"kind_{kind}") if kind is not None else None,
                    "sub_sport": None,
                    "profile_name": r["NAME"],
                    "elapsed_s": (end - start).total_seconds() if end else None,
                    "timer_s": timer,
                    "distance_m": _summary_value(summary, "distanceMeters", "distance"),
                    "calories": int(c) if (c := _summary_value(summary, "caloriesBurnt", "calories")) is not None else None,
                    "avg_hr": int(avg_hr) if avg_hr is not None else None,
                    "max_hr": int(max_hr) if max_hr is not None else None,
                    "min_hr": None,
                    "avg_speed_mps": _summary_value(summary, "averageSpeed"),
                    "max_speed_mps": _summary_value(summary, "maxSpeed"),
                    "ascent_m": _summary_value(summary, "ascentMeters", "elevationGain"),
                    "descent_m": _summary_value(summary, "descentMeters", "elevationLoss"),
                    "avg_cadence": None,
                    "max_cadence": None,
                    "avg_power_w": None,
                    "max_power_w": None,
                    "normalized_power_w": None,
                    "aerobic_te": _summary_value(summary, "trainingEffectAerobic"),
                    "anaerobic_te": _summary_value(summary, "trainingEffectAnaerobic"),
                    "training_load": None,
                    "training_stress_score": None,
                    "avg_temperature_c": None,
                    "avg_respiration": None,
                    "num_laps": None,
                    "hr_zone_times_s": None,
                    "device_serial": device.mac,
                }
            )
        )
    return out


def parse_gadgetbridge_db(path: Path, tz: ZoneInfo) -> tuple[ParsedRows, list[GbDevice]]:
    """Parse every non-Garmin device in the export. Returns rows and the device list."""
    conn = open_readonly(path)
    try:
        devs = devices(conn)
        out = ParsedRows()
        for dev in devs:
            if dev.source == SOURCE_ZEPP:
                parse_zepp_device(conn, dev, tz, out)
        return out, devs
    finally:
        conn.close()
