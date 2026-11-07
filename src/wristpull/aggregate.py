"""Period aggregates (daily, weekly, activity, sleep tables) built with polars."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from .merge import DAILY_METRICS, merged_activities, merged_daily, merged_sleep
from .timeutil import parse_iso_utc

STRAP_METRICS = (
    "steps",
    "hr_min",
    "hr_avg",
    "resting_hr",
    "stress_avg",
    "spo2_avg",
    "temperature_avg",
    "sleep_duration_s",
    "sleep_score",
    "hrv_last_night_avg_ms",
)


def daily_frame(conn: sqlite3.Connection, start: str, end: str) -> pl.DataFrame:
    """One row per date: garmin_* columns (merged USB/Gadgetbridge) and strap_* columns."""
    records: list[dict[str, Any]] = []
    for day in merged_daily(conn, start, end):
        row: dict[str, Any] = {"date": day.date, "sources": "+".join(day.sources)}
        for m in DAILY_METRICS:
            row[f"garmin_{m}"] = day.garmin.get(m) if day.garmin else None
        for m in STRAP_METRICS:
            row[f"strap_{m}"] = day.strap.get(m) if day.strap else None
        records.append(row)
    schema = {"date": pl.Utf8, "sources": pl.Utf8}
    schema.update({f"garmin_{m}": pl.Float64 for m in DAILY_METRICS})
    schema.update({f"strap_{m}": pl.Float64 for m in STRAP_METRICS})
    frame = pl.DataFrame(records, schema=schema) if records else pl.DataFrame(schema=schema)
    return frame.with_columns(pl.col("date").str.to_date().alias("date"))


def weekly_frame(daily: pl.DataFrame) -> pl.DataFrame:
    """ISO-week aggregates of the daily frame (averages per day with data, sums for volumes)."""
    if daily.is_empty():
        return pl.DataFrame()
    d = daily.with_columns(
        pl.col("date").dt.strftime("%G-W%V").alias("week"),
        pl.col("date").dt.strftime("%G-W%V").alias("_w"),
    )
    agg = (
        d.group_by("week", maintain_order=True)
        .agg(
            pl.col("date").min().alias("from"),
            pl.col("date").max().alias("to"),
            pl.col("date").count().alias("days"),
            pl.col("garmin_steps").mean().round(0).alias("steps_per_day"),
            (pl.col("garmin_distance_m").sum() / 1000).round(1).alias("walk_distance_km"),
            pl.col("garmin_resting_hr").mean().round(1).alias("resting_hr"),
            pl.col("garmin_hrv_last_night_avg_ms").mean().round(1).alias("hrv_ms"),
            pl.col("garmin_stress_avg").mean().round(1).alias("stress"),
            pl.col("garmin_body_battery_max").mean().round(0).alias("body_battery_max"),
            (pl.col("garmin_sleep_duration_s").mean() / 3600).round(2).alias("sleep_h"),
            pl.col("garmin_sleep_score").mean().round(0).alias("sleep_score"),
            pl.col("garmin_activities_count").sum().cast(pl.Int64).alias("activities"),
            (pl.col("garmin_activities_duration_s").sum() / 3600).round(2).alias("training_h"),
            (pl.col("garmin_activities_distance_m").sum() / 1000).round(1).alias("training_km"),
            pl.col("garmin_training_load").sum().round(0).alias("training_load"),
            pl.col("strap_resting_hr").mean().round(1).alias("strap_resting_hr"),
            (pl.col("strap_sleep_duration_s").mean() / 3600).round(2).alias("strap_sleep_h"),
            pl.col("strap_hrv_last_night_avg_ms").mean().round(1).alias("strap_hrv_ms"),
            pl.col("strap_stress_avg").mean().round(1).alias("strap_stress"),
        )
        .drop("week")
        .rename({"from": "from"})
    )
    weeks = d.group_by("_w", maintain_order=True).agg(pl.col("date").min()).select(pl.col("_w").alias("week"))
    return pl.concat([weeks, agg], how="horizontal")


def activities_frame(conn: sqlite3.Connection, start: str, end: str, tz: ZoneInfo) -> pl.DataFrame:
    rows = merged_activities(conn, start, end)
    records = []
    for a in rows:
        start_local = parse_iso_utc(a["start_utc"]).astimezone(tz)
        duration = a.get("timer_s") or a.get("elapsed_s")
        records.append(
            {
                "date": a["local_date"],
                "start_local": start_local.strftime("%Y-%m-%d %H:%M"),
                "device": a["device"],
                "sport": a.get("sport"),
                "name": a.get("name") or a.get("profile_name"),
                "duration_min": round(duration / 60, 1) if duration else None,
                "distance_km": round(a["distance_m"] / 1000, 2) if a.get("distance_m") else None,
                "avg_hr": a.get("avg_hr"),
                "max_hr": a.get("max_hr"),
                "calories": a.get("calories"),
                "ascent_m": a.get("ascent_m"),
                "aerobic_te": a.get("aerobic_te"),
                "anaerobic_te": a.get("anaerobic_te"),
                "training_load": round(a["training_load"], 1) if a.get("training_load") is not None else None,
                "hr_zones_min": _zones_minutes(a.get("hr_zone_times_s")),
            }
        )
    schema = {
        "date": pl.Utf8, "start_local": pl.Utf8, "device": pl.Utf8, "sport": pl.Utf8, "name": pl.Utf8,
        "duration_min": pl.Float64, "distance_km": pl.Float64, "avg_hr": pl.Int64, "max_hr": pl.Int64,
        "calories": pl.Int64, "ascent_m": pl.Float64, "aerobic_te": pl.Float64, "anaerobic_te": pl.Float64,
        "training_load": pl.Float64, "hr_zones_min": pl.Utf8,
    }
    return pl.DataFrame(records, schema=schema) if records else pl.DataFrame(schema=schema)


def _zones_minutes(zones_json: str | None) -> str | None:
    if not zones_json:
        return None
    import json

    try:
        zones = json.loads(zones_json)
    except ValueError:
        return None
    mins = [round((z or 0) / 60) for z in zones]
    while mins and mins[-1] == 0:
        mins.pop()
    return "/".join(str(m) for m in mins) if mins else None


def sleep_frame(conn: sqlite3.Connection, start: str, end: str, tz: ZoneInfo) -> pl.DataFrame:
    rows = merged_sleep(conn, start, end)
    records = []
    for s in rows:
        st = parse_iso_utc(s["start_utc"]).astimezone(tz)
        en = parse_iso_utc(s["end_utc"]).astimezone(tz)
        records.append(
            {
                "date": s["local_date"],
                "device": s["device"],
                "bed_local": st.strftime("%Y-%m-%d %H:%M"),
                "wake_local": en.strftime("%Y-%m-%d %H:%M"),
                "sleep_h": round((s.get("duration_s") or 0) / 3600, 2),
                "deep_min": round((s.get("deep_s") or 0) / 60),
                "light_min": round((s.get("light_s") or 0) / 60),
                "rem_min": round((s.get("rem_s") or 0) / 60),
                "awake_min": round((s.get("awake_s") or 0) / 60),
                "score": s.get("score"),
                "awakenings": s.get("awakenings"),
            }
        )
    schema = {
        "date": pl.Utf8, "device": pl.Utf8, "bed_local": pl.Utf8, "wake_local": pl.Utf8, "sleep_h": pl.Float64,
        "deep_min": pl.Int64, "light_min": pl.Int64, "rem_min": pl.Int64, "awake_min": pl.Int64,
        "score": pl.Int64, "awakenings": pl.Int64,
    }
    return pl.DataFrame(records, schema=schema) if records else pl.DataFrame(schema=schema)


def period_overview(daily: pl.DataFrame, activities: pl.DataFrame) -> dict[str, Any]:
    """Headline numbers for the period."""
    if daily.is_empty():
        return {"days": 0}
    g = daily
    out: dict[str, Any] = {
        "days": g.height,
        "days_with_garmin": _days_with_data(g, "garmin_"),
        "days_with_strap": _days_with_data(g, "strap_"),
        "steps_per_day": _r(g["garmin_steps"].mean(), 0),
        "resting_hr": _r(g["garmin_resting_hr"].mean(), 1),
        "resting_hr_min": _r(g["garmin_resting_hr"].min(), 0),
        "resting_hr_max": _r(g["garmin_resting_hr"].max(), 0),
        "hrv_ms": _r(g["garmin_hrv_last_night_avg_ms"].mean(), 1),
        "stress": _r(g["garmin_stress_avg"].mean(), 1),
        "body_battery_max": _r(g["garmin_body_battery_max"].mean(), 0),
        "sleep_h": _r((g["garmin_sleep_duration_s"].mean() or 0) / 3600, 2) if g["garmin_sleep_duration_s"].drop_nulls().len() else None,
        "sleep_score": _r(g["garmin_sleep_score"].mean(), 0),
        "activities": activities.height,
        "training_h": _r((activities["duration_min"].sum() or 0) / 60, 1) if activities.height else 0,
        "training_km": _r(activities["distance_km"].sum(), 1) if activities.height else 0,
        "training_load": _r(activities["training_load"].sum(), 0) if activities.height else 0,
        "strap_resting_hr": _r(g["strap_resting_hr"].mean(), 1),
        "strap_sleep_h": _r((g["strap_sleep_duration_s"].mean() or 0) / 3600, 2) if g["strap_sleep_duration_s"].drop_nulls().len() else None,
        "strap_temperature": _r(g["strap_temperature_avg"].mean(), 2),
    }
    return out


def _days_with_data(frame: pl.DataFrame, prefix: str) -> int:
    cols = [c for c in frame.columns if c.startswith(prefix)]
    if not cols:
        return 0
    return int(frame.select(pl.any_horizontal([pl.col(c).is_not_null() for c in cols]).sum()).item())


def _r(value: Any, digits: int) -> float | int | None:
    if value is None:
        return None
    try:
        return round(float(value), digits) if digits else int(round(float(value)))
    except (TypeError, ValueError):
        return None


def week_bounds(day: date) -> tuple[date, date]:
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def month_bounds(day: date) -> tuple[date, date]:
    start = day.replace(day=1)
    nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, nxt - timedelta(days=1)


def parse_date(text: str) -> date:
    return datetime.strptime(text, "%Y-%m-%d").date()
