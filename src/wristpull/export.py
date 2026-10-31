"""wristpull export --for-claude: a compact Markdown summary plus CSV attachments."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from . import __version__
from .aggregate import activities_frame, daily_frame, period_overview, sleep_frame, weekly_frame
from .config import Config
from .merge import source_coverage
from .timeutil import iso_utc, now_utc, zone


@dataclass
class ExportResult:
    directory: Path
    markdown: str
    files: list[Path] = field(default_factory=list)
    rows: dict[str, int] = field(default_factory=dict)


def _fmt(value: Any, digits: int = 0, suffix: str = "") -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}{suffix}"
    return f"{value}{suffix}"


def _md_table(frame: pl.DataFrame, columns: list[tuple[str, str]], digits: dict[str, int] | None = None) -> str:
    """Render selected columns of a frame as a Markdown table. columns: (column, header)."""
    digits = digits or {}
    header = "| " + " | ".join(h for _, h in columns) + " |"
    sep = "|" + "|".join("---" for _ in columns) + "|"
    lines = [header, sep]
    for row in frame.iter_rows(named=True):
        cells = []
        for col, _ in columns:
            value = row.get(col)
            if hasattr(value, "isoformat"):
                value = value.isoformat()
            cells.append(_fmt(value, digits.get(col, 1)) if isinstance(value, float) else _fmt(value))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def build_markdown(conn: sqlite3.Connection, config: Config, start: str, end: str, *, attachments: list[str]) -> tuple[str, dict[str, pl.DataFrame]]:
    tz = zone(config.timezone)
    daily = daily_frame(conn, start, end)
    weekly = weekly_frame(daily)
    acts = activities_frame(conn, start, end, tz)
    sleep = sleep_frame(conn, start, end, tz)
    overview = period_overview(daily, acts)
    coverage = source_coverage(conn, start, end)

    md: list[str] = []
    md.append(f"# Health data {start} to {end}")
    md.append("")
    md.append(
        f"Generated {iso_utc(now_utc())} by wristpull {__version__}. Local timezone {config.timezone}; "
        f"all dates are local calendar days. Sources: "
        + (", ".join(f"{s} ({n} days)" for s, n in sorted(coverage.items())) or "none")
        + "."
    )
    md.append("")
    md.append("Devices: *Garmin* = fenix 6X Pro (USB and Gadgetbridge merged, USB first), *Strap* = Amazfit Helio Strap via Gadgetbridge.")
    md.append("")
    md.append("## Overview")
    md.append("")
    if overview.get("days"):
        md.append("| Metric | Garmin | Strap |")
        md.append("|---|---|---|")
        md.append(f"| Days with data | {overview['days_with_garmin']} | {overview['days_with_strap']} |")
        md.append(f"| Steps per day | {_fmt(overview['steps_per_day'])} | - |")
        md.append(f"| Resting HR (avg, min-max) | {_fmt(overview['resting_hr'], 1)} ({_fmt(overview['resting_hr_min'])}-{_fmt(overview['resting_hr_max'])}) | {_fmt(overview['strap_resting_hr'], 1)} |")
        md.append(f"| HRV last night avg (ms) | {_fmt(overview['hrv_ms'], 1)} | - |")
        md.append(f"| Stress (avg of daily avg) | {_fmt(overview['stress'], 1)} | - |")
        md.append(f"| Body battery daily max (avg) | {_fmt(overview['body_battery_max'])} | - |")
        md.append(f"| Sleep (h per night) | {_fmt(overview['sleep_h'], 2)} | {_fmt(overview['strap_sleep_h'], 2)} |")
        md.append(f"| Sleep score (avg) | {_fmt(overview['sleep_score'])} | - |")
        md.append(f"| Skin temperature (avg C) | - | {_fmt(overview['strap_temperature'], 2)} |")
        md.append(f"| Activities | {overview['activities']} | |")
        md.append(f"| Training time (h) | {_fmt(overview['training_h'], 1)} | |")
        md.append(f"| Training distance (km) | {_fmt(overview['training_km'], 1)} | |")
        md.append(f"| Training load (sum) | {_fmt(overview['training_load'])} | |")
    else:
        md.append("No data in this period.")
    md.append("")
    if not weekly.is_empty():
        md.append("## Weekly (ISO weeks, Garmin; averages are per day with data)")
        md.append("")
        md.append(
            _md_table(
                weekly,
                [
                    ("week", "Week"), ("days", "Days"), ("steps_per_day", "Steps/day"), ("resting_hr", "Rest HR"),
                    ("hrv_ms", "HRV"), ("stress", "Stress"), ("sleep_h", "Sleep h"), ("sleep_score", "Sleep score"),
                    ("activities", "Acts"), ("training_h", "Train h"), ("training_km", "km"), ("training_load", "Load"),
                    ("strap_resting_hr", "Strap rest HR"), ("strap_sleep_h", "Strap sleep h"),
                ],
                {"steps_per_day": 0, "sleep_score": 0, "training_load": 0, "body_battery_max": 0},
            )
        )
        md.append("")
    if acts.height:
        md.append("## Activities")
        md.append("")
        md.append("HR zones are minutes in zone 1/2/3/4/5 (Garmin zones), training load is Garmin's EPOC-based load.")
        md.append("")
        md.append(
            _md_table(
                acts,
                [
                    ("start_local", "Start"), ("device", "Dev"), ("sport", "Sport"), ("name", "Profile"), ("duration_min", "Min"),
                    ("distance_km", "km"), ("avg_hr", "Avg HR"), ("max_hr", "Max HR"), ("aerobic_te", "TE"), ("training_load", "Load"),
                    ("hr_zones_min", "Zones min"),
                ],
                {"duration_min": 0, "distance_km": 2, "aerobic_te": 1, "training_load": 0},
            )
        )
        md.append("")
    if sleep.height:
        md.append("## Sleep")
        md.append("")
        md.append(
            _md_table(
                sleep,
                [
                    ("date", "Wake date"), ("device", "Dev"), ("bed_local", "Bed"), ("wake_local", "Wake"), ("sleep_h", "Sleep h"),
                    ("deep_min", "Deep"), ("light_min", "Light"), ("rem_min", "REM"), ("awake_min", "Awake"), ("score", "Score"),
                ],
                {"sleep_h": 2},
            )
        )
        md.append("")
    md.append("## Definitions and caveats")
    md.append("")
    md.append("- Steps and distance are the watch's daily totals (cumulative counters, summed over activity types). Strap steps come per minute from Gadgetbridge.")
    md.append("- Resting HR is the watch's own value for the day; HR min/avg/max are over all-day wellness samples, not workouts.")
    md.append("- Stress is Garmin's 0-100 scale; unmeasurable periods are excluded from averages.")
    md.append("- Sleep duration excludes awake time; the night is attributed to the wake-up date.")
    md.append("- Missing values (-) mean the device did not record or was not synced for that day.")
    if attachments:
        md.append("")
        md.append("## Attachments")
        md.append("")
        for name in attachments:
            md.append(f"- `{name}`")
    return "\n".join(md) + "\n", {"daily": daily, "weekly": weekly, "activities": acts, "sleep": sleep}


def export_for_claude(conn: sqlite3.Connection, config: Config, start: str, end: str, *, out_dir: Path | None = None, write_csv: bool = True) -> ExportResult:
    directory = out_dir or (config.data_dir / "exports" / f"{start}_{end}")
    attachments = ["daily.csv", "weekly.csv", "activities.csv", "sleep.csv"] if write_csv else []
    markdown, frames = build_markdown(conn, config, start, end, attachments=attachments)
    directory.mkdir(parents=True, exist_ok=True)
    result = ExportResult(directory=directory, markdown=markdown)
    summary = directory / "summary.md"
    summary.write_text(markdown, "utf-8")
    result.files.append(summary)
    if write_csv:
        for name, frame in frames.items():
            path = directory / f"{name}.csv"
            frame.write_csv(path)
            result.files.append(path)
            result.rows[name] = frame.height
    return result
