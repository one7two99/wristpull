"""Rolling baselines and z-scores on the daily frame (polars, trailing windows, the day itself excluded)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import polars as pl

from ..aggregate import STRAP_METRICS

METRICS = ("hrv_last_night_avg_ms", "resting_hr", "sleep_duration_s", "sleep_score", "body_battery_max", "stress_avg", "training_load", "steps")
SHORT_DAYS = 7
LONG_DAYS = 28
MIN_DAYS = 7
EPS = 1e-9


def merged_frame(daily: pl.DataFrame, metrics: tuple[str, ...] = METRICS) -> pl.DataFrame:
    """``date`` plus one column per metric: the watch's value, the strap's where the watch has none
    (the same fallback as ``coach.metrics.value``); metrics the strap lacks stay watch-only."""
    columns = [pl.col("date")]
    for m in metrics:
        garmin = f"garmin_{m}"
        strap = f"strap_{m}"
        if garmin in daily.columns and strap in daily.columns and m in STRAP_METRICS:
            columns.append(pl.coalesce(pl.col(garmin), pl.col(strap)).alias(m))
        elif garmin in daily.columns:
            columns.append(pl.col(garmin).alias(m))
        else:
            columns.append(pl.lit(None, dtype=pl.Float64).alias(m))
    return daily.select(columns)


def calendar_frame(frame: pl.DataFrame, start: date, end: date) -> pl.DataFrame:
    """One row per calendar day from ``start`` to ``end``; days without data keep nulls."""
    days = pl.DataFrame({"date": [start + timedelta(days=i) for i in range((end - start).days + 1)]})
    joined = days.join(frame.with_columns(pl.col("date").cast(pl.Date)), on="date", how="left")  # an empty frame still brings its columns
    return joined.sort("date")


def rolling_baselines(frame: pl.DataFrame, metrics: tuple[str, ...] = METRICS, *, short: int = SHORT_DAYS, long: int = LONG_DAYS, min_days: int = MIN_DAYS) -> pl.DataFrame:
    """Adds ``<m>_mean7``, ``<m>_mean28``, ``<m>_std28``, ``<m>_median28`` and ``<m>_z`` per metric.

    Windows are trailing and exclude the day itself (``shift(1)``); a window with fewer than ``min_days``
    values gives null, so gaps never count as zero; a constant window gives a null z-score."""
    expressions = []
    for m in metrics:
        if m not in frame.columns:
            continue
        prev = pl.col(m).shift(1)
        mean28 = prev.rolling_mean(window_size=long, min_samples=min_days)
        std28 = prev.rolling_std(window_size=long, min_samples=min_days, ddof=1)
        expressions += [
            prev.rolling_mean(window_size=short, min_samples=min(min_days, short)).alias(f"{m}_mean7"),
            mean28.alias(f"{m}_mean28"),
            std28.alias(f"{m}_std28"),
            prev.rolling_median(window_size=long, min_samples=min_days).alias(f"{m}_median28"),
            pl.when(std28 > EPS).then((pl.col(m) - mean28) / std28).otherwise(None).alias(f"{m}_z"),
        ]
    return frame.with_columns(expressions) if expressions else frame


def rolling_stats(values: list[float | None], *, window: int = LONG_DAYS, min_days: int = MIN_DAYS) -> tuple[list[float | None], list[float | None]]:
    """Trailing mean and standard deviation of the ``window`` values before each position (the position itself
    excluded; fewer than ``min_days`` values give None) for plain lists; the metric pages use this."""
    if not values:
        return [], []
    frame = pl.DataFrame({"v": pl.Series(values, dtype=pl.Float64)})
    prev = pl.col("v").shift(1)
    out = frame.select(
        prev.rolling_mean(window_size=window, min_samples=min_days).alias("mean"),
        prev.rolling_std(window_size=window, min_samples=min_days, ddof=1).alias("std"),
    )
    return out["mean"].to_list(), out["std"].to_list()


def rows_of(frame: pl.DataFrame) -> list[dict[str, Any]]:
    """The frame as dicts with ISO date strings (what the readiness and correlation code consumes)."""
    rows = frame.to_dicts()
    for row in rows:
        if hasattr(row.get("date"), "isoformat"):
            row["date"] = row["date"].isoformat()
    return rows
