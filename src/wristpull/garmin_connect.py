"""Parsers for the JSON files of the Garmin Connect account export.

Two files matter: the daily summaries (``DI-Connect-Aggregator/UDSFile_<from>_<to>.json``, one
record per day in roughly 100-day blocks) and the nights
(``DI-Connect-Wellness/<from>_<to>_<pk>_sleepData.json``). Both are undocumented and change
between export generations, so every field is optional: a missing one becomes NULL, nothing
raises. Records with the FIT epoch as date (1989-12-31 and other placeholders before
``MIN_DATE``) or without any wellness content are discarded and counted.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .rows import ParsedRows, ParsedSleep
from .timeutil import iso_utc

MIN_DATE = "2013-01-01"  # earlier dates are placeholders for broken timestamps
UDS_COLUMNS = (
    "steps", "distance_m", "active_calories", "active_time_s", "moderate_min", "vigorous_min",
    "hr_min", "hr_max", "resting_hr", "stress_avg", "stress_max", "body_battery_min", "body_battery_max",
    "spo2_avg", "spo2_min", "respiration_avg",
)


def gmt_to_iso(value: Any) -> str | None:
    """``"2026-07-25T22:46:00.0"`` (Connect's GMT strings) -> ``"2026-07-25T22:46:00Z"``; None when unusable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return iso_utc(datetime.fromisoformat(value).replace(tzinfo=UTC))
    except ValueError:
        return None


def _num(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _int(value: Any) -> int | None:
    v = _num(value)
    return int(round(v)) if v is not None else None


def _round(value: Any, digits: int = 1) -> float | None:
    v = _num(value)
    return round(v, digits) if v is not None else None


def _load_list(data: bytes) -> list[dict[str, Any]]:
    obj = json.loads(data)
    if isinstance(obj, dict):
        obj = [obj]
    return [r for r in obj if isinstance(r, dict)]


# --- daily summaries (UDS) --------------------------------------------------------------------


def _stress(rec: dict[str, Any]) -> tuple[float | None, int | None]:
    stress = rec.get("allDayStress") or {}
    totals = [a for a in stress.get("aggregatorList") or [] if isinstance(a, dict) and a.get("type") == "TOTAL"]
    if not totals:
        return None, None
    return _round(totals[0].get("averageStressLevel")), _int(totals[0].get("maxStressLevel"))


def _body_battery(rec: dict[str, Any]) -> tuple[int | None, int | None]:
    stats = (rec.get("bodyBattery") or {}).get("bodyBatteryStatList") or []
    low = high = None
    for s in stats:
        if not isinstance(s, dict):
            continue
        if s.get("bodyBatteryStatType") == "LOWEST":
            low = _int(s.get("statsValue"))
        elif s.get("bodyBatteryStatType") == "HIGHEST":
            high = _int(s.get("statsValue"))
    return low, high


def _is_junk(rec: dict[str, Any], date: str | None) -> bool:
    if not date or date < MIN_DATE:
        return True
    has_values = any(_num(rec.get(k)) is not None for k in ("totalSteps", "restingHeartRate", "minHeartRate", "totalDistanceMeters"))
    return not has_values and not rec.get("includesWellnessData")


def parse_uds(data: bytes, *, source: str, tz_name: str) -> tuple[list[dict[str, Any]], int]:
    """Rows for ``connect_daily`` (one per day, ordered by date) and the number of discarded records."""
    rows: list[dict[str, Any]] = []
    discarded = 0
    for rec in _load_list(data):
        date = rec.get("calendarDate") if isinstance(rec.get("calendarDate"), str) else None
        if _is_junk(rec, date):
            discarded += 1
            continue
        stress_avg, stress_max = _stress(rec)
        bb_min, bb_max = _body_battery(rec)
        active = _num(rec.get("activeSeconds"))
        highly = _num(rec.get("highlyActiveSeconds"))
        rows.append(
            {
                "source": source,
                "local_date": date,
                "tz": tz_name,
                "steps": _int(rec.get("totalSteps")),
                "distance_m": _num(rec.get("totalDistanceMeters")),
                "active_calories": _int(rec.get("activeKilocalories")),
                "active_time_s": (active or 0) + (highly or 0) if active is not None or highly is not None else None,
                "moderate_min": _int(rec.get("moderateIntensityMinutes")),
                "vigorous_min": _int(rec.get("vigorousIntensityMinutes")),
                "hr_min": _int(rec.get("minHeartRate")),
                "hr_max": _int(rec.get("maxHeartRate")),
                "resting_hr": _int(rec.get("restingHeartRate")),
                "stress_avg": stress_avg,
                "stress_max": stress_max,
                "body_battery_min": bb_min,
                "body_battery_max": bb_max,
                "spo2_avg": _round(rec.get("averageSpo2Value")),
                "spo2_min": _int(rec.get("lowestSpo2Value")),
                "respiration_avg": _round((rec.get("respiration") or {}).get("avgWakingRespirationValue")),
            }
        )
    rows.sort(key=lambda r: r["local_date"])
    return rows, discarded


# --- nights --------------------------------------------------------------------------------------


def parse_sleep_json(data: bytes, *, source: str, tz_name: str) -> tuple[ParsedRows, int]:
    """Sleep sessions as ``ParsedRows`` (no stage timeline: the export only has stage totals) and the discarded count."""
    out = ParsedRows()
    discarded = 0
    for rec in _load_list(data):
        start = gmt_to_iso(rec.get("sleepStartTimestampGMT"))
        end = gmt_to_iso(rec.get("sleepEndTimestampGMT"))
        date = rec.get("calendarDate") if isinstance(rec.get("calendarDate"), str) else (end[:10] if end else None)
        if not start or not end or not date or date < MIN_DATE or end <= start:
            discarded += 1
            continue
        scores = rec.get("sleepScores") or {}
        duration = int((datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds())
        row: dict[str, Any] = {
            "source": source,
            "start_utc": start,
            "end_utc": end,
            "tz": tz_name,
            "local_date": date,
            "duration_s": duration,
            "deep_s": _int(rec.get("deepSleepSeconds")),
            "light_s": _int(rec.get("lightSleepSeconds")),
            "rem_s": _int(rec.get("remSleepSeconds")),
            "awake_s": _int(rec.get("awakeSleepSeconds")),
            "unmeasurable_s": _int(rec.get("unmeasurableSeconds")),
            "score": _int(scores.get("overallScore")) if isinstance(scores, dict) else None,
            "quality_score": _int(scores.get("qualityScore")) if isinstance(scores, dict) else None,
            "recovery_score": _int(scores.get("recoveryScore")) if isinstance(scores, dict) else None,
            "awakenings": _int(rec.get("awakeCount")),
            "avg_stress": _round(rec.get("avgSleepStress")),
        }
        out.sleep.append(ParsedSleep(row={k: v for k, v in row.items() if v is not None or k in ("source", "start_utc", "end_utc", "tz", "local_date")}))
    out.sleep.sort(key=lambda n: n.row["start_utc"])
    return out, discarded


# --- activity summaries and VO2max history (phase 3) ------------------------------------------

# Connect's activityType -> FIT sports a matching activity may carry (None: do not check the sport)
ACTIVITY_SPORTS: dict[str, tuple[str, ...] | None] = {
    "running": ("running",), "trail_running": ("running",), "treadmill_running": ("running",), "track_running": ("running",),
    "walking": ("walking", "hiking"), "casual_walking": ("walking", "hiking"), "speed_walking": ("walking", "hiking"),
    "hiking": ("hiking", "walking"), "mountaineering": ("hiking", "walking", "mountaineering"),
    "cycling": ("cycling",), "indoor_cycling": ("cycling",), "virtual_ride": ("cycling",), "mountain_biking": ("cycling",), "road_biking": ("cycling",), "gravel_cycling": ("cycling",),
    "strength_training": ("training", "fitness_equipment"), "indoor_cardio": ("training", "fitness_equipment"), "fitness_equipment": ("training", "fitness_equipment"),
    "elliptical": ("training", "fitness_equipment"), "stair_climbing": ("training", "fitness_equipment"), "indoor_rowing": ("rowing", "training", "fitness_equipment"),
    "yoga": ("training", "fitness_equipment"), "pilates": ("training", "fitness_equipment"), "hiit": ("training", "fitness_equipment"), "breathwork": ("training", "fitness_equipment"),
    "lap_swimming": ("swimming",), "open_water_swimming": ("swimming",), "rowing": ("rowing",), "other": None, "stop_watch": None,
}


def _ms_to_iso(value: Any) -> str | None:
    v = _num(value)
    if v is None or v <= 0:
        return None
    return iso_utc(datetime.fromtimestamp(v / 1000.0, UTC))


def parse_activity_summaries(data: bytes) -> list[dict[str, Any]]:
    """``summarizedActivities.json`` -> ``{start_utc, activity_type, name, training_load}`` per activity, by start."""
    obj = json.loads(data)
    records: list[dict[str, Any]] = []
    for block in (obj if isinstance(obj, list) else [obj]):
        if isinstance(block, dict) and isinstance(block.get("summarizedActivitiesExport"), list):
            records.extend(r for r in block["summarizedActivitiesExport"] if isinstance(r, dict))
        elif isinstance(block, dict) and "activityId" in block:
            records.append(block)
    out: list[dict[str, Any]] = []
    for rec in records:
        start = _ms_to_iso(rec.get("startTimeGmt")) or _ms_to_iso(rec.get("beginTimestamp"))
        name = rec.get("name") if isinstance(rec.get("name"), str) else None
        if not start or not name or not name.strip():
            continue
        out.append({"start_utc": start, "activity_type": rec.get("activityType") if isinstance(rec.get("activityType"), str) else None, "name": name.strip(), "training_load": _num(rec.get("activityTrainingLoad"))})
    out.sort(key=lambda r: r["start_utc"])
    return out


def sport_matches(activity_type: str | None, sport: str | None) -> bool:
    """Whether a Connect activity type and a FIT sport can describe the same activity (unknowns match)."""
    if not activity_type or not sport:
        return True
    allowed = ACTIVITY_SPORTS.get(activity_type, ())
    if allowed is None:
        return True
    return sport in allowed if allowed else sport == activity_type


def parse_maxmet(data: bytes, *, source: str, tz_name: str) -> list[dict[str, Any]]:
    """``MetricsMaxMetData_*.json`` -> ``metrics`` rows (``vo2_max`` per day, timestamp from the record)."""
    rows: list[dict[str, Any]] = []
    for rec in _load_list(data):
        date = rec.get("calendarDate") if isinstance(rec.get("calendarDate"), str) else None
        value = _num(rec.get("vo2MaxValue"))
        if not date or date < MIN_DATE or value is None or value <= 0:
            continue
        ts = gmt_to_iso(rec.get("updateTimestamp")) or f"{date}T12:00:00Z"
        rows.append({"source": source, "ts_utc": ts, "tz": tz_name, "local_date": date, "metric": "vo2_max", "value": round(value, 1), "sport": None})
    rows.sort(key=lambda r: r["ts_utc"])
    return rows


def parse_biometrics(data: bytes, *, source: str, tz_name: str) -> list[dict[str, Any]]:
    """``userBioMetrics.json`` -> ``metrics`` rows ``weight_kg`` (grams in the export), one per day (last entry wins).

    The static profile file (``userBioMetricProfileData.json``, no dates) is not a history and is ignored.
    """
    by_day: dict[str, tuple[int, float]] = {}
    for rec in _load_list(data):
        meta = rec.get("metaData") or {}
        date = meta.get("calendarDate") if isinstance(meta.get("calendarDate"), str) else None
        weight = rec.get("weight") or {}
        grams = _num(weight.get("weight")) if isinstance(weight, dict) else _num(weight)
        if not date or date[:10] < MIN_DATE or grams is None or grams <= 0:
            continue
        day = date[:10]
        seq = _int(meta.get("sequence")) or 0
        if day not in by_day or seq >= by_day[day][0]:
            by_day[day] = (seq, round(grams / 1000.0, 1))
    return [{"source": source, "ts_utc": f"{day}T12:00:00Z", "tz": tz_name, "local_date": day, "metric": "weight_kg", "value": value, "sport": None} for day, (_seq, value) in sorted(by_day.items())]
