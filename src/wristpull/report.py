"""wristpull report week|month: a readable Markdown report with optional SVG charts."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from .aggregate import activities_frame, daily_frame, month_bounds, period_overview, sleep_frame, week_bounds
from .charts import bar_chart, line_chart
from .config import Config
from .export import _fmt, _md_table
from .timeutil import zone


@dataclass
class ReportResult:
    directory: Path
    markdown: str
    start: date
    end: date
    files: list[Path] = field(default_factory=list)


def _delta(current, previous, digits: int = 1, unit: str = "") -> str:
    if current is None or previous is None:
        return ""
    diff = float(current) - float(previous)
    sign = "+" if diff > 0 else ""
    return f" ({sign}{diff:.{digits}f}{unit} vs previous period)"


def build_report(conn: sqlite3.Connection, config: Config, kind: str, day: date, *, charts: bool, chart_dir: Path | None) -> tuple[str, list[tuple[str, str]]]:
    tz = zone(config.timezone)
    start, end = week_bounds(day) if kind == "week" else month_bounds(day)
    prev_end = start - timedelta(days=1)
    prev_start = week_bounds(prev_end)[0] if kind == "week" else month_bounds(prev_end)[0]
    s, e = start.isoformat(), end.isoformat()
    daily = daily_frame(conn, s, e)
    acts = activities_frame(conn, s, e, tz)
    sleep = sleep_frame(conn, s, e, tz)
    overview = period_overview(daily, acts)
    prev = period_overview(daily_frame(conn, prev_start.isoformat(), prev_end.isoformat()), activities_frame(conn, prev_start.isoformat(), prev_end.isoformat(), tz))

    title = f"Week {start.strftime('%G-W%V')}" if kind == "week" else start.strftime("%B %Y")
    md: list[str] = [f"# {title} ({s} to {e})", ""]
    if not overview.get("days"):
        md.append("No data for this period.")
        return "\n".join(md) + "\n", []

    md.append("## Summary")
    md.append("")
    md.append(f"- Days with Garmin data: {overview['days_with_garmin']} of {(end - start).days + 1}; with Strap data: {overview['days_with_strap']}.")
    if overview["steps_per_day"] is not None:
        md.append(f"- Steps: {_fmt(overview['steps_per_day'])} per day{_delta(overview['steps_per_day'], prev.get('steps_per_day'), 0)}.")
    if overview["resting_hr"] is not None:
        md.append(f"- Resting HR: {_fmt(overview['resting_hr'], 1)} bpm, range {_fmt(overview['resting_hr_min'])}-{_fmt(overview['resting_hr_max'])}{_delta(overview['resting_hr'], prev.get('resting_hr'), 1, ' bpm')}.")
    if overview["hrv_ms"] is not None:
        md.append(f"- HRV (nightly avg): {_fmt(overview['hrv_ms'], 1)} ms{_delta(overview['hrv_ms'], prev.get('hrv_ms'), 1, ' ms')}.")
    if overview["stress"] is not None:
        md.append(f"- Stress: {_fmt(overview['stress'], 1)}{_delta(overview['stress'], prev.get('stress'), 1)}; body battery daily max {_fmt(overview['body_battery_max'])}.")
    if overview["sleep_h"] is not None:
        md.append(f"- Sleep: {_fmt(overview['sleep_h'], 2)} h per night, score {_fmt(overview['sleep_score'])}{_delta(overview['sleep_h'], prev.get('sleep_h'), 2, ' h')}.")
    if overview["strap_sleep_h"] is not None or overview["strap_resting_hr"] is not None:
        md.append(f"- Strap: resting HR {_fmt(overview['strap_resting_hr'], 1)} bpm, sleep {_fmt(overview['strap_sleep_h'], 2)} h, skin temperature {_fmt(overview['strap_temperature'], 2)} C.")
    md.append(f"- Training: {overview['activities']} activities, {_fmt(overview['training_h'], 1)} h, {_fmt(overview['training_km'], 1)} km, load {_fmt(overview['training_load'])}{_delta(overview['training_load'], prev.get('training_load'), 0)}.")
    md.append("")

    svgs: list[tuple[str, str]] = []
    if charts and not daily.is_empty():
        labels = [d.strftime("%d.%m") if kind == "month" else d.strftime("%a %d") for d in daily["date"].to_list()]

        def has(*cols: str) -> bool:
            return any(daily[c].drop_nulls().len() for c in cols)

        if has("garmin_steps"):
            svgs.append(("steps.svg", bar_chart("Steps per day", labels, daily["garmin_steps"].to_list())))
        if has("garmin_resting_hr", "strap_resting_hr"):
            svgs.append(("resting_hr.svg", line_chart("Resting heart rate", labels, {"Garmin": daily["garmin_resting_hr"].to_list(), "Strap": daily["strap_resting_hr"].to_list()}, unit=" bpm")))
        if has("garmin_sleep_duration_s"):
            sleep_h = [None if v is None else round(v / 3600, 2) for v in daily["garmin_sleep_duration_s"].to_list()]
            svgs.append(("sleep.svg", bar_chart("Sleep hours", labels, sleep_h, unit=" h", color="#6f42c1")))
        if has("garmin_stress_avg", "garmin_body_battery_max"):
            svgs.append(("stress.svg", line_chart("Stress and body battery", labels, {"Stress avg": daily["garmin_stress_avg"].to_list(), "Body battery max": daily["garmin_body_battery_max"].to_list()})))
        if svgs:
            md.append("## Charts")
            md.append("")
            for name, _ in svgs:
                md.append(f"![{name[:-4]}]({name})")
            md.append("")

    md.append("## Days")
    md.append("")
    day_cols = [
        ("date", "Date"), ("garmin_steps", "Steps"), ("garmin_resting_hr", "Rest HR"), ("garmin_hr_min", "HR min"),
        ("garmin_stress_avg", "Stress"), ("garmin_body_battery_max", "BB max"), ("garmin_sleep_h", "Sleep h"), ("garmin_sleep_score", "Score"),
        ("garmin_activities_count", "Acts"), ("garmin_training_load", "Load"), ("strap_resting_hr", "Strap RHR"), ("strap_sleep_h", "Strap sleep"),
    ]
    table = daily.with_columns(
        (pl.col("garmin_sleep_duration_s") / 3600).round(2).alias("garmin_sleep_h"),
        (pl.col("strap_sleep_duration_s") / 3600).round(2).alias("strap_sleep_h"),
    )
    md.append(_md_table(table, day_cols, {"garmin_steps": 0, "garmin_resting_hr": 0, "garmin_hr_min": 0, "garmin_stress_avg": 0, "garmin_body_battery_max": 0, "garmin_sleep_h": 2, "garmin_sleep_score": 0, "garmin_activities_count": 0, "garmin_training_load": 0, "strap_resting_hr": 0, "strap_sleep_h": 2}))
    md.append("")
    if acts.height:
        md.append("## Activities")
        md.append("")
        md.append(_md_table(acts, [("start_local", "Start"), ("sport", "Sport"), ("name", "Profile"), ("duration_min", "Min"), ("distance_km", "km"), ("avg_hr", "Avg HR"), ("max_hr", "Max HR"), ("aerobic_te", "TE"), ("training_load", "Load"), ("hr_zones_min", "Zones min")], {"duration_min": 0, "distance_km": 2, "aerobic_te": 1, "training_load": 0}))
        md.append("")
    if sleep.height:
        md.append("## Nights")
        md.append("")
        md.append(_md_table(sleep, [("date", "Wake date"), ("device", "Dev"), ("bed_local", "Bed"), ("wake_local", "Wake"), ("sleep_h", "Sleep h"), ("deep_min", "Deep"), ("light_min", "Light"), ("rem_min", "REM"), ("awake_min", "Awake"), ("score", "Score")], {"sleep_h": 2}))
        md.append("")
    return "\n".join(md) + "\n", svgs


def write_report(conn: sqlite3.Connection, config: Config, kind: str, day: date, *, charts: bool = False, out_dir: Path | None = None) -> ReportResult:
    start, end = week_bounds(day) if kind == "week" else month_bounds(day)
    label = start.strftime("%G-W%V") if kind == "week" else start.strftime("%Y-%m")
    directory = out_dir or (config.data_dir / "reports" / f"{kind}-{label}")
    markdown, svgs = build_report(conn, config, kind, day, charts=charts, chart_dir=directory)
    directory.mkdir(parents=True, exist_ok=True)
    result = ReportResult(directory=directory, markdown=markdown, start=start, end=end)
    path = directory / "report.md"
    path.write_text(markdown, "utf-8")
    result.files.append(path)
    for name, svg in svgs:
        p = directory / name
        p.write_text(svg, "utf-8")
        result.files.append(p)
    return result
