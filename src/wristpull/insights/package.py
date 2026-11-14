"""``build_insights``: the one dict the Insights page and the coach's insights report both consume."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from ..aggregate import daily_frame
from ..config import Config
from ..series import rolling_acwr, weekday_pattern
from . import correlate, readiness
from .baselines import METRICS, calendar_frame, merged_frame, rolling_baselines, rows_of
from .sleep import nights, sleep_consistency
from .store import tags_between

PERIOD_DAYS = 90
WARMUP_DAYS = 28
WEEKDAY_METRICS = ("resting_hr", "hrv_last_night_avg_ms", "sleep_duration_s", "stress_avg", "steps")


def build_insights(conn: sqlite3.Connection, config: Config, today: date, *, period_days: int = PERIOD_DAYS, warmup_days: int = WARMUP_DAYS, pairs: tuple[tuple[str, str, int], ...] = correlate.PAIRS) -> dict[str, Any]:
    """Everything computed over ``period_days`` ending today (plus ``warmup_days`` before it for the baselines).

    Numbers are rounded here so the charts, the page and the coach's "numbers used" agree."""
    tz = ZoneInfo(config.timezone)
    start = today - timedelta(days=period_days - 1)
    history = start - timedelta(days=warmup_days)
    frame = calendar_frame(merged_frame(daily_frame(conn, history.isoformat(), today.isoformat())), history, today)
    # a worn day without activities is a day with zero training load, not a gap
    worn = pl.col("resting_hr").is_not_null() | pl.col("steps").is_not_null() | pl.col("sleep_duration_s").is_not_null()
    frame = frame.with_columns(pl.when(pl.col("training_load").is_null() & worn).then(0.0).otherwise(pl.col("training_load")).alias("training_load"))
    frame = rolling_baselines(frame, METRICS)
    rows = rows_of(frame)
    present = [r.get("resting_hr") is not None or r.get("steps") is not None or r.get("sleep_duration_s") is not None for r in rows]
    acwr = rolling_acwr([r["date"] for r in rows], [r.get("training_load") for r in rows], present=present)
    for row, value in zip(rows, acwr):
        row["acwr"] = value
    night_rows = nights(conn, history, today, tz)
    by_night = {n["date"]: n for n in night_rows}
    for row in rows:
        night = by_night.get(row["date"])
        row["bed_offset"] = float(night["bed_offset"]) if night else None
    _z_for_bed_offset(rows)
    ready = readiness.readiness_series(rows)
    window = [i for i, r in enumerate(rows) if r["date"] >= start.isoformat()]
    first = window[0] if window else len(rows)
    latest = next((r for r in reversed(ready) if r["score"] is not None), None)
    tags_by_date = tags_between(conn, history.isoformat(), today.isoformat())
    package: dict[str, Any] = {
        "period": {"start": start.isoformat(), "end": today.isoformat(), "days": period_days, "warmup_days": warmup_days,
                   "days_with_data": sum(1 for i in window if present[i])},
        "readiness": {
            "latest": latest,
            "series": {"dates": [r["date"] for r in ready[first:]], "score": [r["score"] for r in ready[first:]], "confidence": [r["confidence"] for r in ready[first:]]},
        },
        "baselines": {m: _latest_baseline(rows, m) for m in METRICS},
        "load": {"dates": [r["date"] for r in rows[first:]], "acwr": acwr[first:], "load": [r.get("training_load") for r in rows[first:]]},
        "sleep": {**sleep_consistency(night_rows, today, target_h=config.coach.sleep_target_h),
                  "nights": [{"date": n["date"], "bed": n["bed_offset"], "wake": n["wake_min"], "sleep_h": n["sleep_h"], "score": n["score"]} for n in night_rows if n["date"] >= start.isoformat()]},
        "correlations": [correlate.correlation(rows, x, y, lag) for x, y, lag in pairs],
        "tags": correlate.tag_effects(rows, tags_by_date),
        "tag_days": {d: sorted(t) for d, t in tags_by_date.items() if d >= start.isoformat()},
        "weekday": {m: weekday_pattern([r["date"] for r in rows[first:]], [r.get(m) for r in rows[first:]]) for m in WEEKDAY_METRICS},
        "caveats": _caveats(rows, window, night_rows),
    }
    return package


def _z_for_bed_offset(rows: list[dict[str, Any]]) -> None:
    """Bedtime as a z-score against the own trailing 28 nights (same rule as the metrics)."""
    from .baselines import rolling_stats

    values = [r.get("bed_offset") for r in rows]
    means, stds = rolling_stats(values)
    for row, v, m, s in zip(rows, values, means, stds):
        row["bed_offset_z"] = round((v - m) / s, 2) if v is not None and m is not None and s is not None and s > 1e-9 else None


def _latest_baseline(rows: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    row = next((r for r in reversed(rows) if r.get(metric) is not None), None)
    if row is None:
        return {"date": None, "latest": None, "mean7": None, "mean28": None, "std28": None, "z": None}
    r = lambda v, d=1: round(v, d) if v is not None else None  # noqa: E731
    return {"date": row["date"], "latest": r(row.get(metric)), "mean7": r(row.get(f"{metric}_mean7")), "mean28": r(row.get(f"{metric}_mean28")), "std28": r(row.get(f"{metric}_std28")), "z": r(row.get(f"{metric}_z"), 2)}


def _caveats(rows: list[dict[str, Any]], window: list[int], night_rows: list[dict[str, Any]]) -> list[str]:
    out = [
        "Sleep columns of a date describe the night that ended on that date: sleep -> same-day resting HR, Body Battery and stress is lag 0; sleep -> the next night's HRV is lag 1.",
        "Correlations use within-person z-scores against the own trailing 28-day baseline; a correlation is not a cause.",
    ]
    missing = sum(1 for i in window if rows[i].get("resting_hr") is None and rows[i].get("steps") is None)
    if missing:
        out.append(f"{missing} of {len(window)} days in the period have no watch data.")
    devices = {n["device"] for n in night_rows}
    if len(devices) > 1:
        out.append("Nights come from both the watch and the strap; their scales may differ.")
    return out
