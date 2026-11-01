"""Deterministic training-load and recovery metrics computed before anything reaches the model.

All functions take the rows of ``aggregate.daily_frame`` as plain dicts (``date`` as ISO string,
``garmin_<metric>`` / ``strap_<metric>`` columns) sorted by date.
"""

from __future__ import annotations

import statistics
from typing import Any

Row = dict[str, Any]


def value(row: Row, metric: str) -> float | None:
    """Garmin value of a metric, falling back to the strap."""
    v = row.get(f"garmin_{metric}")
    if v is None:
        v = row.get(f"strap_{metric}")
    return float(v) if v is not None else None


def mean(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def baseline(rows: list[Row], metric: str, days: int, *, before: str | None = None) -> float | None:
    """Mean of the last ``days`` rows (optionally strictly before a date) that have the metric."""
    chosen = [r for r in rows if before is None or r["date"] < before][-days:]
    return mean([value(r, metric) for r in chosen])


def daily_load(rows: list[Row]) -> list[float]:
    """Training load per day (0 when nothing was recorded) in row order."""
    return [value(r, "training_load") or 0.0 for r in rows]


def acute_chronic(rows: list[Row], *, acute_days: int = 7, chronic_days: int = 28) -> dict[str, Any]:
    """Acute (7-day sum) vs chronic (28-day weekly mean) training load, plus monotony of the acute week."""
    loads = daily_load(rows)
    acute_window = loads[-acute_days:]
    acute = sum(acute_window)
    history_days = len(rows)
    chronic_window = loads[-chronic_days:]
    chronic_weekly = sum(chronic_window) / max(1, len(chronic_window)) * acute_days if chronic_window else 0.0
    ratio = round(acute / chronic_weekly, 2) if chronic_weekly > 0 else None
    monotony = None
    if len(acute_window) >= 3:
        spread = statistics.pstdev(acute_window)
        monotony = round(statistics.mean(acute_window) / spread, 2) if spread > 0 else None
    return {
        "acute_load": round(acute),
        "chronic_weekly_load": round(chronic_weekly),
        "acwr": ratio,
        "monotony": monotony,
        "history_days": history_days,
        "sufficient_history": history_days >= chronic_days,
    }


def deltas(rows: list[Row], metric: str) -> dict[str, float | None]:
    """Latest value and its 7-/28-day baselines (baselines exclude the latest day)."""
    if not rows:
        return {"latest": None, "mean_7d": None, "mean_28d": None, "delta_7d": None, "delta_28d": None}
    latest_row = rows[-1]
    latest = value(latest_row, metric)
    m7 = baseline(rows, metric, 7, before=latest_row["date"])
    m28 = baseline(rows, metric, 28, before=latest_row["date"])
    return {
        "latest": latest,
        "mean_7d": round(m7, 1) if m7 is not None else None,
        "mean_28d": round(m28, 1) if m28 is not None else None,
        "delta_7d": round(latest - m7, 1) if latest is not None and m7 is not None else None,
        "delta_28d": round(latest - m28, 1) if latest is not None and m28 is not None else None,
    }


def recovery_flags(rows: list[Row], load: dict[str, Any] | None = None) -> list[str]:
    """Plain-language warning signs for the latest day, derived with fixed thresholds."""
    if not rows:
        return []
    flags: list[str] = []
    hrv = deltas(rows, "hrv_last_night_avg_ms")
    if hrv["latest"] is not None and hrv["mean_28d"] and hrv["latest"] < 0.9 * hrv["mean_28d"]:
        flags.append(f"HRV {hrv['latest']:.0f} ms is more than 10 % below the 28-day mean of {hrv['mean_28d']:.0f} ms")
    rhr = deltas(rows, "resting_hr")
    if rhr["delta_28d"] is not None and rhr["delta_28d"] >= 5:
        flags.append(f"resting HR {rhr['latest']:.0f} bpm is {rhr['delta_28d']:+.0f} bpm above the 28-day mean")
    latest = rows[-1]
    sleep_s = value(latest, "sleep_duration_s")
    if sleep_s is not None and sleep_s < 6 * 3600:
        flags.append(f"short sleep: {sleep_s / 3600:.1f} h")
    score = value(latest, "sleep_score")
    if score is not None and score < 60:
        flags.append(f"low sleep score: {score:.0f}")
    bb = latest.get("garmin_body_battery_max")
    if bb is not None and bb < 50:
        flags.append(f"body battery peaked at only {bb:.0f}")
    load = load or acute_chronic(rows)
    if load.get("sufficient_history") and load.get("acwr") is not None and load["acwr"] > 1.3:
        flags.append(f"acute:chronic workload ratio {load['acwr']:.2f} is above 1.3")
    return flags
