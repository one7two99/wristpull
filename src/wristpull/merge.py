"""Combine the sources into a device view.

* Garmin: ``garmin_usb`` and ``gadgetbridge_garmin`` are the same watch. Daily
  metrics are coalesced column by column (USB first, Gadgetbridge fills gaps);
  activities and sleep sessions are de-duplicated by start time.
* Helio Strap (``gadgetbridge_zepp``) is a separate device and stays separate.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

GARMIN_PRIORITY = ("garmin_usb", "gadgetbridge_garmin", "garmin_connect")  # the account export only fills gaps
STRAP_SOURCE = "gadgetbridge_zepp"
DEVICES = {"garmin": GARMIN_PRIORITY, "strap": (STRAP_SOURCE,)}

DAILY_METRICS = (
    "steps",
    "distance_m",
    "active_calories",
    "active_time_s",
    "moderate_min",
    "vigorous_min",
    "hr_min",
    "hr_max",
    "hr_avg",
    "resting_hr",
    "stress_avg",
    "stress_max",
    "body_battery_min",
    "body_battery_max",
    "spo2_avg",
    "spo2_min",
    "respiration_avg",
    "temperature_avg",
    "sleep_duration_s",
    "sleep_score",
    "hrv_last_night_avg_ms",
    "activities_count",
    "activities_duration_s",
    "activities_distance_m",
    "training_load",
)


@dataclass
class MergedDay:
    date: str
    garmin: dict[str, Any] | None = None
    strap: dict[str, Any] | None = None
    sources: list[str] = field(default_factory=list)


def _coalesce(rows: list[sqlite3.Row], priority: tuple[str, ...]) -> dict[str, Any] | None:
    ordered = sorted(rows, key=lambda r: priority.index(r["source"]) if r["source"] in priority else 99)
    if not ordered:
        return None
    merged: dict[str, Any] = {}
    for metric in DAILY_METRICS:
        merged[metric] = next((r[metric] for r in ordered if r[metric] is not None), None)
    # counts/sums for activities must not double count: take them from the highest-priority source with data
    return merged


def merged_daily(conn: sqlite3.Connection, start: str, end: str) -> list[MergedDay]:
    rows = conn.execute(
        "SELECT * FROM daily_summary WHERE local_date BETWEEN ? AND ? ORDER BY local_date, source", (start, end)
    ).fetchall()
    by_date: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_date.setdefault(r["local_date"], []).append(r)
    out: list[MergedDay] = []
    for date in sorted(by_date):
        day_rows = by_date[date]
        day = MergedDay(date=date, sources=sorted({r["source"] for r in day_rows}))
        day.garmin = _coalesce([r for r in day_rows if r["source"] in GARMIN_PRIORITY], GARMIN_PRIORITY)
        day.strap = _coalesce([r for r in day_rows if r["source"] == STRAP_SOURCE], (STRAP_SOURCE,))
        out.append(day)
    return out


def _dedup_by_start(rows: list[sqlite3.Row], priority: tuple[str, ...], key: str = "start_utc") -> list[dict[str, Any]]:
    """Keep one row per start minute, preferring the earlier source in ``priority``."""
    chosen: dict[str, sqlite3.Row] = {}
    for r in rows:
        minute = (r[key] or "")[:16]
        current = chosen.get(minute)
        if current is None or priority.index(r["source"]) < priority.index(current["source"]):
            chosen[minute] = r
    return [dict(r) for r in sorted(chosen.values(), key=lambda r: r[key])]


def merged_activities(conn: sqlite3.Connection, start: str, end: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM activities WHERE local_date BETWEEN ? AND ? ORDER BY start_utc", (start, end)
    ).fetchall()
    garmin = _dedup_by_start([r for r in rows if r["source"] in GARMIN_PRIORITY], GARMIN_PRIORITY)
    strap = [dict(r) for r in rows if r["source"] == STRAP_SOURCE]
    for a in garmin:
        a["device"] = "garmin"
    for a in strap:
        a["device"] = "strap"
    return sorted(garmin + strap, key=lambda a: a["start_utc"])


def merged_sleep(conn: sqlite3.Connection, start: str, end: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM sleep WHERE local_date BETWEEN ? AND ? ORDER BY start_utc", (start, end)).fetchall()
    garmin = _dedup_by_start([r for r in rows if r["source"] in GARMIN_PRIORITY], GARMIN_PRIORITY)
    strap = [dict(r) for r in rows if r["source"] == STRAP_SOURCE]
    for s in garmin:
        s["device"] = "garmin"
    for s in strap:
        s["device"] = "strap"
    return sorted(garmin + strap, key=lambda s: s["start_utc"])


def source_coverage(conn: sqlite3.Connection, start: str, end: str) -> dict[str, int]:
    """Days with any data per source inside the range."""
    rows = conn.execute(
        "SELECT source, COUNT(*) FROM daily_summary WHERE local_date BETWEEN ? AND ? GROUP BY source", (start, end)
    ).fetchall()
    return {r[0]: int(r[1]) for r in rows}
