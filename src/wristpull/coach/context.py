"""Build the data package the coach sees: aggregates only, with precomputed trends and flags."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import polars as pl

from ..aggregate import activities_frame, daily_frame, period_overview, sleep_frame, week_bounds, weekly_frame
from ..config import Config
from ..export import _fmt
from ..timeutil import zone
from . import metrics
from .backends import estimate_tokens

DATA_CAVEATS = [
    "Sleep stage, HRV and SpO2 mappings were verified on one real night only; treat them as plausible, not certified.",
    "If another app synced the watch first, hours of wellness samples (HR, stress, body battery, SpO2) may be missing for that day; missing is not zero.",
]


@dataclass
class Section:
    title: str
    body: str
    priority: int  # higher = dropped later when trimming to the token budget


@dataclass
class DataPackage:
    kind: str
    start: date
    end: date
    sections: list[Section] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)

    def add(self, title: str, body: str, priority: int) -> None:
        if body.strip():
            self.sections.append(Section(title, body.strip(), priority))


def meaningful_activity(a: dict[str, Any]) -> bool:
    """Skip accidental starts: under 5 minutes without distance or heart rate (e.g. the strap's 'kind_0' blips)."""
    duration = a.get("duration_min") or 0
    return duration >= 5 or bool(a.get("distance_km")) or a.get("avg_hr") is not None


def meaningful_frame(acts: pl.DataFrame) -> pl.DataFrame:
    """The same rule as meaningful_activity, applied to an activities_frame."""
    if acts.is_empty():
        return acts
    return acts.filter((pl.col("duration_min").fill_null(0) >= 5) | pl.col("distance_km").is_not_null() | pl.col("avg_hr").is_not_null())


def day_rows(conn: sqlite3.Connection, start: date, end: date) -> list[dict[str, Any]]:
    rows = daily_frame(conn, start.isoformat(), end.isoformat()).to_dicts()
    for r in rows:
        r["date"] = r["date"].isoformat()
    return rows


def _h(seconds: float | None) -> str:
    return "-" if seconds is None else f"{seconds / 3600:.1f} h"


def _row_table(rows: list[dict[str, Any]]) -> str:
    header = "| date | steps | resting HR | HRV ms | stress | body battery max | sleep | load |\n|---|---|---|---|---|---|---|---|"
    lines = [header]
    for r in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    r["date"],
                    _fmt(metrics.value(r, "steps")),
                    _fmt(metrics.value(r, "resting_hr")),
                    _fmt(metrics.value(r, "hrv_last_night_avg_ms"), 0),
                    _fmt(metrics.value(r, "stress_avg"), 0),
                    _fmt(r.get("garmin_body_battery_max")),
                    _h(metrics.value(r, "sleep_duration_s")),
                    _fmt(metrics.value(r, "training_load"), 0),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def _delta_line(label: str, d: dict[str, float | None], unit: str) -> str:
    if d["latest"] is None:
        return f"- {label}: no value for the latest day"
    parts = [f"{d['latest']:.0f}{unit}"]
    if d["mean_7d"] is not None:
        parts.append(f"7-day mean {d['mean_7d']:.0f}{unit} from {d['n_7d']} earlier day{'s' if d['n_7d'] != 1 else ''}")
    if d["mean_28d"] is not None:
        parts.append(f"28-day mean {d['mean_28d']:.0f}{unit} from {d['n_28d']} day{'s' if d['n_28d'] != 1 else ''} (Δ {d['delta_28d']:+.0f})")
    if d["mean_7d"] is None:
        parts.append("no earlier days to compare with")
    return f"- {label}: " + ", ".join(parts)


def build_daily(conn: sqlite3.Connection, config: Config, today: date, *, digest: str = "", journal: str | None = None, history_days: int = 35) -> DataPackage:
    """Last night, yesterday, the 7-day table, baselines, load and flags for the morning check."""
    tz = zone(config.timezone)
    start = today - timedelta(days=history_days - 1)
    rows = day_rows(conn, start, today)
    pkg = DataPackage("daily", start, today)
    with_garmin = sum(1 for r in rows[-7:] if any(v is not None for k, v in r.items() if k.startswith("garmin_")))
    with_strap = sum(1 for r in rows[-7:] if any(v is not None for k, v in r.items() if k.startswith("strap_")))
    pkg.facts.update(today=today.isoformat(), days_with_garmin_7d=with_garmin, days_with_strap_7d=with_strap)
    pkg.add("Coverage", f"Today is {today.isoformat()} ({today.strftime('%A')}), time zone {config.timezone}. Garmin data on {with_garmin} of the last 7 days, strap data on {with_strap}.", 100)

    nights = sleep_frame(conn, today.isoformat(), today.isoformat(), tz)
    if nights.height:
        lines = []
        for n in nights.iter_rows(named=True):
            lines.append(
                f"- {n['device']}: in bed {n['bed_local'][11:]} to {n['wake_local'][11:]}, slept {n['sleep_h']:.1f} h "
                f"(deep {n['deep_min']} min, light {n['light_min']} min, REM {n['rem_min']} min, awake {n['awake_min']} min), score {_fmt(n['score'])}"
            )
        pkg.add("Last night", "\n".join(lines), 90)
    else:
        pkg.add("Last night", "No sleep record for last night.", 90)

    yesterday = today - timedelta(days=1)
    y_rows = [r for r in rows if r["date"] == yesterday.isoformat()]
    if y_rows:
        y = y_rows[0]
        mod, vig = y.get("garmin_moderate_min"), y.get("garmin_vigorous_min")
        intensity = "-" if mod is None and vig is None else f"{int((mod or 0) + (vig or 0))} min"
        pkg.add(
            "Yesterday",
            f"- steps {_fmt(metrics.value(y, 'steps'))}, resting HR {_fmt(metrics.value(y, 'resting_hr'))} bpm, stress avg {_fmt(metrics.value(y, 'stress_avg'), 0)}, "
            f"body battery {_fmt(y.get('garmin_body_battery_min'))}–{_fmt(y.get('garmin_body_battery_max'))}, intensity minutes {intensity}, training load {_fmt(metrics.value(y, 'training_load'), 0)}",
            85,
        )
    else:
        pkg.add("Yesterday", "No daily summary for yesterday.", 85)

    acts = [a for a in activities_frame(conn, (today - timedelta(days=7)).isoformat(), today.isoformat(), tz).iter_rows(named=True) if meaningful_activity(a)]
    if acts:
        lines = [
            f"- {a['start_local']} {a['sport'] or '-'}{(' (' + a['name'] + ')') if a['name'] else ''}: {_fmt(a['duration_min'], 0)} min, "
            f"{_fmt(a['distance_km'], 1)} km, avg HR {_fmt(a['avg_hr'])}, max {_fmt(a['max_hr'])}, load {_fmt(a['training_load'], 0)}, HR zones min {a['hr_zones_min'] or '-'}"
            for a in acts
        ]
        pkg.add("Activities, last 7 days", "\n".join(lines), 60)
    else:
        pkg.add("Activities, last 7 days", "No recorded activities in the last 7 days.", 60)

    pkg.add("Last 7 days", _row_table(rows[-7:]), 70)

    trend = [
        _delta_line("Resting HR", metrics.deltas(rows, "resting_hr"), " bpm"),
        _delta_line("HRV (night)", metrics.deltas(rows, "hrv_last_night_avg_ms"), " ms"),
        _delta_line("Stress", metrics.deltas(rows, "stress_avg"), ""),
        _delta_line("Sleep score", metrics.deltas(rows, "sleep_score"), ""),
    ]
    pkg.add("Trends (latest day vs baselines)", "\n".join(trend), 80)

    load = metrics.acute_chronic(rows)
    pkg.facts["load"] = load
    load_text = f"- acute 7-day load {load['acute_load']}, chronic weekly load {load['chronic_weekly_load']}, ACWR {load['acwr'] if load['acwr'] is not None else '-'}, monotony {load['monotony'] if load['monotony'] is not None else '-'}"
    if not load["sufficient_history"]:
        load_text += f"\n- only {load['history_days']} days of history; the chronic load is not yet meaningful"
    pkg.add("Training load", load_text, 75)

    pkg.flags = metrics.recovery_flags(rows, load)
    pkg.add("Warning flags", "\n".join(f"- {f}" for f in pkg.flags) if pkg.flags else "- none", 95)
    if journal:
        pkg.add("Athlete's journal", journal, 88)
    if digest:
        pkg.add("Previous coaching", digest, 50)
    caveats = list(DATA_CAVEATS)
    missing = [r["date"] for r in rows[-7:] if metrics.value(r, "resting_hr") is None]
    if missing:
        caveats.append("Days without a resting-HR value in the last week: " + ", ".join(missing))
    if len(rows) < 7:
        caveats.append(f"Only {len(rows)} days with any data in the last {history_days} days.")
    pkg.add("Data caveats", "\n".join(f"- {c}" for c in caveats), 40)
    return pkg


def review_week(day: date) -> tuple[date, date]:
    """The week a review run on ``day`` covers: the current ISO week on a Sunday, else the last completed one."""
    if day.weekday() == 6:
        return week_bounds(day)
    return week_bounds(day - timedelta(days=day.weekday() + 1))


def _delta(current: Any, previous: Any, digits: int = 0, unit: str = "") -> str:
    if current is None:
        return "-"
    text = f"{current:.{digits}f}{unit}"
    if previous is not None:
        text += f" (prev {previous:.{digits}f}{unit}, Δ {current - previous:+.{digits}f})"
    return text


def build_weekly(conn: sqlite3.Connection, config: Config, day: date, *, digest: str = "", weeks_back: int = 6) -> DataPackage:
    """The completed week vs the one before, with activities, nights, a 6-week trend and the load history."""
    tz = zone(config.timezone)
    start, end = review_week(day)
    prev_start, prev_end = start - timedelta(days=7), start - timedelta(days=1)
    pkg = DataPackage("weekly", start, end)
    rows_all = day_rows(conn, start - timedelta(days=7 * weeks_back - 1), end)
    week_rows = [r for r in rows_all if start.isoformat() <= r["date"] <= end.isoformat()]
    daily = daily_frame(conn, start.isoformat(), end.isoformat())
    prev_daily = daily_frame(conn, prev_start.isoformat(), prev_end.isoformat())
    acts = meaningful_frame(activities_frame(conn, start.isoformat(), end.isoformat(), tz))
    prev_acts = meaningful_frame(activities_frame(conn, prev_start.isoformat(), prev_end.isoformat(), tz))
    now, prev = period_overview(daily, acts), period_overview(prev_daily, prev_acts)
    pkg.facts.update(week=f"{start.isoformat()} to {end.isoformat()}", days_with_data=now.get("days", 0), previous_days_with_data=prev.get("days", 0))
    pkg.add("Coverage", f"Review of {start.isoformat()} to {end.isoformat()} (ISO week {start.isocalendar().week}), run on {day.isoformat()}, time zone {config.timezone}. Days with data: {now.get('days', 0)} (previous week {prev.get('days', 0)}).", 100)
    if now.get("days"):
        overview = [
            f"- steps per day: {_delta(now.get('steps_per_day'), prev.get('steps_per_day'))}",
            f"- resting HR: {_delta(now.get('resting_hr'), prev.get('resting_hr'), 1, ' bpm')}",
            f"- HRV (night): {_delta(now.get('hrv_ms'), prev.get('hrv_ms'), 1, ' ms')}",
            f"- stress: {_delta(now.get('stress'), prev.get('stress'), 1)}",
            f"- sleep: {_delta(now.get('sleep_h'), prev.get('sleep_h'), 1, ' h')}, score {_delta(now.get('sleep_score'), prev.get('sleep_score'))}",
            f"- training: {now.get('activities', 0)} activities (prev {prev.get('activities', 0)}), {_delta(now.get('training_h'), prev.get('training_h'), 1, ' h')}, {_delta(now.get('training_km'), prev.get('training_km'), 1, ' km')}, load {_delta(now.get('training_load'), prev.get('training_load'))}",
        ]
        pkg.add("Week vs previous week", "\n".join(overview), 95)
    else:
        pkg.add("Week vs previous week", "No daily data in the review week.", 95)
    pkg.add("Days of the week", _row_table(week_rows) if week_rows else "No daily rows.", 85)
    acts_rows = list(acts.iter_rows(named=True))[:15]
    if acts_rows:
        lines = [
            f"- {a['start_local']} {a['sport'] or '-'}{(' (' + a['name'] + ')') if a['name'] else ''}: {_fmt(a['duration_min'], 0)} min, {_fmt(a['distance_km'], 1)} km, "
            f"avg HR {_fmt(a['avg_hr'])}, max {_fmt(a['max_hr'])}, load {_fmt(a['training_load'], 0)}, TE {_fmt(a['aerobic_te'], 1)}/{_fmt(a['anaerobic_te'], 1)}, HR zones min {a['hr_zones_min'] or '-'}"
            for a in acts_rows
        ]
        pkg.add("Activities", "\n".join(lines), 80)
    else:
        pkg.add("Activities", "No recorded activities this week.", 80)
    nights = sleep_frame(conn, start.isoformat(), end.isoformat(), tz)
    if nights.height:
        lines = [
            f"- {n['date']} {n['device']}: {n['bed_local'][11:]}–{n['wake_local'][11:]}, {n['sleep_h']:.1f} h, deep {n['deep_min']} / light {n['light_min']} / REM {n['rem_min']} / awake {n['awake_min']} min, score {_fmt(n['score'])}"
            for n in nights.iter_rows(named=True)
        ]
        pkg.add("Nights", "\n".join(lines), 70)
    weekly = weekly_frame(daily_frame(conn, (start - timedelta(days=7 * (weeks_back - 1))).isoformat(), end.isoformat()))
    if weekly.height:
        lines = ["| week | days | steps/day | resting HR | HRV ms | sleep h | activities | training h | load |", "|---|---|---|---|---|---|---|---|---|"]
        for w in weekly.iter_rows(named=True):
            lines.append(f"| {w['week']} | {w['days']} | {_fmt(w['steps_per_day'], 0)} | {_fmt(w['resting_hr'], 1)} | {_fmt(w['hrv_ms'], 1)} | {_fmt(w['sleep_h'], 1)} | {_fmt(w['activities'])} | {_fmt(w['training_h'], 1)} | {_fmt(w['training_load'], 0)} |")
        pkg.add(f"Last {weeks_back} weeks", "\n".join(lines), 65)
    load = metrics.acute_chronic(rows_all)
    pkg.facts["load"] = load
    load_text = f"- acute 7-day load {load['acute_load']}, chronic weekly load {load['chronic_weekly_load']}, ACWR {load['acwr'] if load['acwr'] is not None else '-'}, monotony {load['monotony'] if load['monotony'] is not None else '-'}"
    if not load["sufficient_history"]:
        load_text += f"\n- only {load['history_days']} days of history; the chronic load is not yet meaningful"
    pkg.add("Training load", load_text, 75)
    pkg.flags = metrics.recovery_flags(rows_all, load)
    pkg.add("Warning flags (latest day)", "\n".join(f"- {f}" for f in pkg.flags) if pkg.flags else "- none", 90)
    if digest:
        pkg.add("Previous coaching", digest, 50)
    caveats = list(DATA_CAVEATS)
    if now.get("days", 0) < 7:
        caveats.append(f"Only {now.get('days', 0)} of 7 days have data in the review week.")
    pkg.add("Data caveats", "\n".join(f"- {c}" for c in caveats), 40)
    return pkg


def render(pkg: DataPackage, *, budget_tokens: int) -> tuple[str, list[str]]:
    """Markdown of the package within the budget; lowest-priority sections are dropped first."""
    sections = list(pkg.sections)
    dropped: list[str] = []

    def text(parts: list[Section]) -> str:
        return "\n\n".join(f"## {s.title}\n\n{s.body}" for s in sorted(parts, key=lambda s: -s.priority))

    while sections and estimate_tokens(text(sections)) > budget_tokens and len(sections) > 1:
        victim = min(sections, key=lambda s: s.priority)
        sections.remove(victim)
        dropped.append(victim.title)
    out = text(sections)
    if dropped:
        out += "\n\n_(omitted for size: " + ", ".join(dropped) + ")_"
    return out, dropped
