"""Sleep regularity and debt from the nights themselves (bedtime and wake-up in the night's own time zone)."""

from __future__ import annotations

import sqlite3
import statistics
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..merge import merged_sleep
from ..timeutil import parse_iso_utc

MAIN_NIGHT_S = 3 * 3600
AXIS_START_MIN = 18 * 60  # bedtimes are measured from 18:00, so a night around midnight does not wrap
DEFAULT_TARGET_H = 7.5
WINDOW_DAYS = 14


def clock_minutes(ts_utc: str, tz_name: str | None, default_tz: ZoneInfo) -> int:
    """Local minutes since midnight of a UTC stamp in the night's own zone (falls back to the configured one)."""
    try:
        zone = ZoneInfo(tz_name) if tz_name else default_tz
    except (KeyError, ValueError):
        zone = default_tz
    local = parse_iso_utc(ts_utc).astimezone(zone)
    return local.hour * 60 + local.minute


def bed_offset(minutes: int) -> int:
    """Minutes since 18:00 (23:30 -> 330, 00:30 -> 390): no midnight wrap in the spread."""
    return (minutes - AXIS_START_MIN) % (24 * 60)


def nights(conn: sqlite3.Connection, start: date, end: date, default_tz: ZoneInfo) -> list[dict[str, Any]]:
    """One main night per wake-up date: the watch's longest record, the strap's only for dates the watch lacks."""
    by_date: dict[str, dict[str, Any]] = {}
    for s in merged_sleep(conn, start.isoformat(), end.isoformat()):
        if (s.get("duration_s") or 0) < MAIN_NIGHT_S:
            continue
        current = by_date.get(s["local_date"])
        better = current is None or (s["device"] == "garmin" and current["device"] != "garmin") or (s["device"] == current["device"] and (s.get("duration_s") or 0) > (current.get("duration_s") or 0))
        if better:
            by_date[s["local_date"]] = s
    out = []
    for day in sorted(by_date):
        s = by_date[day]
        bed = clock_minutes(s["start_utc"], s.get("tz"), default_tz)
        wake = clock_minutes(s["end_utc"], s.get("tz"), default_tz)
        out.append({
            "date": day,
            "device": s["device"],
            "bed_min": bed,
            "wake_min": wake,
            "bed_offset": bed_offset(bed),
            "sleep_h": round((s.get("duration_s") or 0) / 3600, 2),
            "score": s.get("score"),
            "recovery_score": s.get("recovery_score"),
            "avg_stress": s.get("avg_stress"),
        })
    return out


def _clock(minutes: float) -> str:
    total = int(round(minutes)) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def sleep_consistency(nights_rows: list[dict[str, Any]], end: date, *, window_days: int = WINDOW_DAYS, target_h: float = DEFAULT_TARGET_H) -> dict[str, Any]:
    """Spread of bedtime and wake-up (minutes), their means as clock times, sleep debt against the target
    over the last ``window_days`` days ending on ``end``."""
    since = (end - timedelta(days=window_days - 1)).isoformat()
    chosen = [n for n in nights_rows if since <= n["date"] <= end.isoformat()]
    n = len(chosen)
    if n == 0:
        return {"n_nights": 0, "nights_missing": window_days, "window_days": window_days, "target_h": target_h, "bedtime_std_min": None, "wake_std_min": None,
                "bedtime_mean": None, "wake_mean": None, "mean_h": None, "debt_h": None, "balance_h": None}
    beds = [n_["bed_offset"] for n_ in chosen]
    wakes = [n_["wake_min"] for n_ in chosen]
    hours = [n_["sleep_h"] for n_ in chosen]
    return {
        "n_nights": n,
        "nights_missing": window_days - n,
        "window_days": window_days,
        "target_h": target_h,
        "bedtime_std_min": round(statistics.pstdev(beds)) if n > 1 else 0,
        "wake_std_min": round(statistics.pstdev(wakes)) if n > 1 else 0,
        "bedtime_mean": _clock(statistics.mean(beds) + AXIS_START_MIN),
        "wake_mean": _clock(statistics.mean(wakes)),
        "mean_h": round(statistics.mean(hours), 2),
        "debt_h": round(sum(max(0.0, target_h - h) for h in hours), 1),
        "balance_h": round(sum(h - target_h for h in hours), 1),
    }
