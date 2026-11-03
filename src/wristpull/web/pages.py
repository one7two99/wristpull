"""What each dashboard page shows, assembled from library functions (no SQL here).

The functions return plain dicts and lists; the templates format and translate them, and the
chart series go to ECharts as JSON (missing values stay ``None`` -> ``null``, drawn as gaps).
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..aggregate import daily_frame, weekly_frame
from ..coach.metrics import acute_chronic, deltas, recovery_flags, value
from ..coach.store import recent_notes, recent_runs
from ..config import Config
from ..detail import activity_detail, downsample, local_clock, sleep_night, sports
from ..merge import STRAP_SOURCE, merged_activities, merged_sleep
from ..status import last_imports
from ..timeutil import parse_iso_utc
from ..tui.queries import day_rows
from ..workouts.model import MAX_NAME_LEN, Workout, describe_steps, template
from ..workouts.store import list_workouts, push_status, save, workouts_dir

# (metric, label msgid, unit, display kind, which direction is good)
TILES: tuple[tuple[str, str, str, str, str | None], ...] = (
    ("resting_hr", "Resting HR", "bpm", "int", "down"),
    ("hrv_last_night_avg_ms", "HRV last night", "ms", "int", "up"),
    ("sleep_duration_s", "Sleep", "h", "hm", "up"),
    ("stress_avg", "Stress", "avg", "int", "down"),
    ("steps", "Steps", "", "int", "up"),
    ("body_battery_max", "Body Battery", "max", "int", "up"),
)
TREND_WEEKS = (4, 12, 52)
# raw_files.source -> label msgid shown as "last sync"
SOURCE_LABELS = {"garmin_usb": "Watch (USB)", "gadgetbridge_garmin": "Watch (phone)", "gadgetbridge": "Strap (phone)"}
COACH_EXCERPT_CHARS = 700


def _r(v: float | None, digits: int = 1) -> float | None:
    return round(float(v), digits) if v is not None else None


def tone(delta: float | None, good: str | None, *, tolerance: float = 0.5) -> str:
    """``good``, ``bad`` or ``neutral`` for a change against the baseline."""
    if delta is None or good is None or abs(delta) < tolerance:
        return "neutral"
    return "good" if (delta < 0) == (good == "down") else "bad"


def coach_blocks(markdown: str) -> list[dict[str, str]]:
    """The coach answer as headings and paragraphs (``## Status`` -> a heading), no HTML involved."""
    blocks: list[dict[str, str]] = []
    for line in markdown.splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith("#"):
            blocks.append({"kind": "heading", "text": text.lstrip("#").strip()})
        elif blocks and blocks[-1]["kind"] == "text":
            blocks[-1]["text"] += "\n" + text
        else:
            blocks.append({"kind": "text", "text": text})
    return blocks


def today_data(conn: sqlite3.Connection, today: date) -> dict[str, Any]:
    """Tiles against the 28-day baseline, recovery flags, load, the latest coach check, last syncs."""
    rows = day_rows(conn, today - timedelta(days=60), today)
    tiles = []
    for metric, label, unit, kind, good in TILES:
        d = deltas(rows, metric)
        scale = 3600 if metric == "sleep_duration_s" else 1
        tolerance = 0.25 if metric == "sleep_duration_s" else (200 if metric == "steps" else 1)
        delta = d["delta_28d"] / scale if d["delta_28d"] is not None else None
        tiles.append(
            {
                "metric": metric,
                "label": label,
                "unit": unit,
                "kind": kind,
                "value": d["latest"] / scale if d["latest"] is not None else None,
                "mean_28d": d["mean_28d"] / scale if d["mean_28d"] is not None else None,
                "delta_28d": delta,
                "tone": tone(delta, good, tolerance=tolerance),
                "n_28d": d["n_28d"],
            }
        )
    load = acute_chronic(rows) if rows else {}
    runs = recent_runs(conn, kind="daily", limit=1)
    coach = None
    if runs:
        run = runs[0]
        text = (run.get("answer_md") or "").strip()
        coach = {
            "id": run["id"],
            "created_at_utc": run.get("created_at_utc"),
            "backend": run.get("backend"),
            "model": run.get("model"),
            "rating": run.get("rating"),
            "excerpt": text[:COACH_EXCERPT_CHARS] + ("…" if len(text) > COACH_EXCERPT_CHARS else ""),
            "blocks": coach_blocks(text[:COACH_EXCERPT_CHARS] + ("…" if len(text) > COACH_EXCERPT_CHARS else "")),
        }
    return {
        "as_of": rows[-1]["date"] if rows else None,
        "tiles": tiles,
        "sleep_score": value(rows[-1], "sleep_score") if rows else None,
        "flags": recovery_flags(rows, load) if rows else [],
        "load": load,
        "coach": coach,
        "imports": last_imports(conn),
    }


def acwr_series(rows: list[dict[str, Any]], days: list[str]) -> list[float | None]:
    """ACWR as of each day (only once 28 days of history exist, like the coach)."""
    out: list[float | None] = []
    for day in days:
        upto = [r for r in rows if r["date"] <= day]
        load = acute_chronic(upto) if upto else {}
        out.append(load.get("acwr") if load.get("sufficient_history") else None)
    return out


def trends_data(conn: sqlite3.Connection, today: date, weeks: int) -> dict[str, Any]:
    """Daily series for the last ``weeks`` weeks plus weekly training volume, ready for ECharts."""
    weeks = weeks if weeks in TREND_WEEKS else TREND_WEEKS[0]
    start = today - timedelta(days=7 * weeks - 1)
    rows = day_rows(conn, start - timedelta(days=35), today)  # history before the window feeds the ACWR
    by_day = {r["date"]: r for r in rows}
    days = [(start + timedelta(days=i)).isoformat() for i in range((today - start).days + 1)]

    def col(name: str, scale: float = 1.0, digits: int = 1) -> list[float | None]:
        return [_r(by_day[d][name] / scale, digits) if d in by_day and by_day[d].get(name) is not None else None for d in days]

    weekly = weekly_frame(daily_frame(conn, start.isoformat(), today.isoformat()))
    week_rows = weekly.to_dicts() if not weekly.is_empty() else []
    charts = [
        {"id": "rhr", "title": "Resting HR", "unit": "bpm", "kind": "line", "labels": days,
         "series": [{"name": "Watch", "data": col("garmin_resting_hr")}, {"name": "Strap", "data": col("strap_resting_hr")}]},
        {"id": "hrv", "title": "HRV last night", "unit": "ms", "kind": "line", "labels": days,
         "series": [{"name": "Watch", "data": col("garmin_hrv_last_night_avg_ms")}, {"name": "Strap", "data": col("strap_hrv_last_night_avg_ms")}]},
        {"id": "sleep", "title": "Sleep", "unit": "h", "kind": "bar", "labels": days,
         "series": [{"name": "Watch", "data": col("garmin_sleep_duration_s", 3600, 2)}, {"name": "Strap", "data": col("strap_sleep_duration_s", 3600, 2)}]},
        {"id": "stress", "title": "Stress", "unit": "avg", "kind": "line", "labels": days,
         "series": [{"name": "Watch", "data": col("garmin_stress_avg")}, {"name": "Strap", "data": col("strap_stress_avg")}]},
        {"id": "volume", "title": "Training per week", "unit": "h", "kind": "bar", "labels": [str(w["week"]) for w in week_rows],
         "series": [{"name": "Hours", "data": [_r(w.get("training_h"), 2) for w in week_rows]}]},
        {"id": "acwr", "title": "Acute:chronic load", "unit": "", "kind": "acwr", "labels": days,
         "series": [{"name": "ACWR", "data": acwr_series(rows, days)}], "band": [0.8, 1.3]},
    ]
    return {"weeks": weeks, "start": start.isoformat(), "end": today.isoformat(), "days_with_data": sum(1 for d in days if d in by_day), "charts": charts}


# ---- activities and sleep (wave D2) ---------------------------------------------------------

DEVICE_LABELS = {"garmin": "Watch", "strap": "Strap"}
STAGE_ORDER = ("deep", "light", "rem", "awake", "unmeasurable")
CURVE_POINTS = 400


def _period(today: date, weeks: int) -> tuple[date, int]:
    weeks = weeks if weeks in TREND_WEEKS else TREND_WEEKS[0]
    return today - timedelta(days=7 * weeks - 1), weeks


def activity_title(row: dict[str, Any]) -> str:
    name = (row.get("profile_name") or "").strip()
    sport = (row.get("sport") or "activity").replace("_", " ")
    return name if name and name.lower() != sport.lower() else sport.capitalize()


def activities_list(conn: sqlite3.Connection, today: date, weeks: int, *, sport: str | None = None, tz: ZoneInfo) -> dict[str, Any]:
    """Merged activities (Garmin USB/phone duplicates removed, strap separate), newest first."""
    start, weeks = _period(today, weeks)
    rows = list(reversed(merged_activities(conn, start.isoformat(), today.isoformat())))
    if sport:
        rows = [r for r in rows if (r.get("sport") or "") == sport]
    items = [
        {
            "id": r["id"],
            "date": r["local_date"],
            "clock": local_clock(r["start_utc"], tz),
            "title": activity_title(r),
            "sport": r.get("sport"),
            "device": DEVICE_LABELS.get(r.get("device", ""), r.get("device") or ""),
            "duration_s": r.get("timer_s") or r.get("elapsed_s"),
            "distance_m": r.get("distance_m") or None,
            "avg_hr": r.get("avg_hr"),
            "training_load": r.get("training_load"),
        }
        for r in rows
    ]
    return {
        "weeks": weeks,
        "start": start.isoformat(),
        "sport": sport,
        "sports": sports(conn),
        "items": items,
        "total_s": sum(i["duration_s"] or 0 for i in items),
        "total_m": sum(i["distance_m"] or 0 for i in items),
    }


def activity_page(conn: sqlite3.Connection, activity_id: int, tz: ZoneInfo) -> dict[str, Any] | None:
    """One activity: key numbers, HR and altitude curves over elapsed minutes, zones, laps."""
    detail = activity_detail(conn, activity_id)
    if detail is None:
        return None
    records = detail.pop("records")
    t0 = parse_iso_utc(records[0]["ts_utc"]) if records else None
    minutes = [round((parse_iso_utc(r["ts_utc"]) - t0).total_seconds() / 60, 1) for r in records] if t0 else []
    hr = downsample([r["hr"] for r in records], CURVE_POINTS)
    alt = downsample([r["altitude_m"] for r in records], CURVE_POINTS)
    labels = [str(round(m)) if m is not None else "" for m in downsample(minutes, CURVE_POINTS)]
    charts = []
    if any(v is not None for v in hr):
        charts.append({"id": "hr", "title": "Heart rate", "unit": "bpm", "kind": "line", "labels": labels,
                       "series": [{"name": "Heart rate", "data": [_r(v, 0) for v in hr]}]})
    if any(v is not None for v in alt):
        charts.append({"id": "alt", "title": "Altitude", "unit": "m", "kind": "line", "labels": labels,
                       "series": [{"name": "Altitude", "data": [_r(v, 0) for v in alt]}]})
    zones_total = sum(detail["hr_zones_s"]) or 0
    zones = [{"label": f"Z{i}", "seconds": s, "pct": round(100 * s / zones_total) if zones_total else 0}
             for i, s in enumerate(detail["hr_zones_s"], 1)] if zones_total else []
    return {
        "a": detail,
        "title": activity_title(detail),
        "clock": local_clock(detail["start_utc"], tz),
        "duration_s": detail.get("timer_s") or detail.get("elapsed_s"),
        "charts": charts,
        "zones": zones,
        "laps": detail["laps"],
    }


def sleep_list(conn: sqlite3.Connection, today: date, weeks: int) -> dict[str, Any]:
    """One row per wake-up date with the watch's and the strap's night side by side, newest first."""
    start, weeks = _period(today, weeks)
    by_day: dict[str, dict[str, Any]] = {}
    for s in merged_sleep(conn, start.isoformat(), today.isoformat()):
        day = by_day.setdefault(s["local_date"], {"date": s["local_date"], "watch": None, "strap": None})
        key = "watch" if s["device"] == "garmin" else "strap"
        if day[key] is None or (s.get("duration_s") or 0) > (day[key].get("duration_s") or 0):
            day[key] = s  # the main night if a device logged a nap too
    rows = sorted(by_day.values(), key=lambda d: d["date"], reverse=True)
    return {"weeks": weeks, "start": start.isoformat(), "rows": rows}


def stage_band(stages: list[dict[str, Any]], start_utc: str, end_utc: str) -> list[dict[str, Any]]:
    """Stage segments as percentages of the night for a horizontal band."""
    t0, t1 = parse_iso_utc(start_utc), parse_iso_utc(end_utc)
    span = (t1 - t0).total_seconds() or 1
    out = []
    for st in stages:
        a = max(0.0, (parse_iso_utc(st["start_utc"]) - t0).total_seconds())
        b = min(span, (parse_iso_utc(st["end_utc"]) - t0).total_seconds())
        if b > a:
            out.append({"stage": st["stage"], "left": round(100 * a / span, 2), "width": round(100 * (b - a) / span, 2)})
    return out


def night_page(conn: sqlite3.Connection, sleep_id: int, tz: ZoneInfo) -> dict[str, Any] | None:
    """One night: stage band and totals, HRV/SpO2/respiration/HR in the window, the other device's night."""
    night = sleep_night(conn, sleep_id)
    if night is None:
        return None
    same_day = merged_sleep(conn, night["local_date"], night["local_date"])
    this = next((s for s in same_day if s["id"] == sleep_id), None)
    device = this["device"] if this else ("strap" if night["source"] == STRAP_SOURCE else "garmin")
    other = max((s for s in same_day if s["device"] != device), key=lambda s: s.get("duration_s") or 0, default=None)
    stage_totals = [{"stage": k, "seconds": night.get(f"{k}_s")} for k in STAGE_ORDER if night.get(f"{k}_s")]
    return {
        "n": night,
        "device": DEVICE_LABELS.get(device, device),
        "bed": local_clock(night["start_utc"], tz),
        "wake": local_clock(night["end_utc"], tz),
        "band": stage_band(night["stages"], night["start_utc"], night["end_utc"]),
        "stage_totals": stage_totals,
        "other": other,
        "other_device": DEVICE_LABELS.get(other["device"], "") if other else None,
    }


# ---- coach, journal, workouts (wave D3) -------------------------------------------------------

COACH_KINDS = ("daily", "weekly", "ask")
TEMPLATE_KINDS = ("time", "distance", "intervals")


def coach_data(conn: sqlite3.Connection, tz: ZoneInfo, *, runs: int = 10, notes: int = 10) -> dict[str, Any]:
    """Recent successful runs with their answer as blocks, and the latest journal notes."""
    items = []
    for run in recent_runs(conn, limit=runs):
        stamp = run.get("created_at_utc")
        items.append({
            "id": run["id"],
            "kind": run["kind"],
            "when": parse_iso_utc(stamp).astimezone(tz).strftime("%d.%m. %H:%M") if stamp else "–",
            "backend": run.get("backend"),
            "model": run.get("model"),
            "question": run.get("question"),
            "rating": run.get("rating"),
            "cost_usd": run.get("cost_usd"),
            "blocks": coach_blocks(run.get("answer_md") or ""),
        })
    return {"runs": items, "notes": recent_notes(conn, notes)}


def workouts_data(config: Config, conn: sqlite3.Connection, today: date) -> dict[str, Any]:
    """Workout files from a week ago on, with the push state from the history and validation errors."""
    rows = []
    for w in list_workouts(config, start=today - timedelta(days=7)):
        state = (push_status(conn, w.path or "") if w.path else None) or w.status
        total = w.total_seconds
        rows.append({
            "file": Path(w.path).name if w.path else "",
            "name": w.name,
            "day": w.day.isoformat() if w.day else None,
            "sport": w.sport,
            "steps": len(w.steps),
            "minutes": total // 60 if total else None,
            "metres": w.total_metres or None,
            "state": state,
            "errors": list(w.errors),
        })
    return {"rows": rows, "folder": str(workouts_dir(config)), "kinds": TEMPLATE_KINDS, "tomorrow": (today + timedelta(days=1)).isoformat()}


def check_lines(workout: Workout) -> dict[str, Any]:
    """What the live check shows: the steps as the watch will show them, plus every problem."""
    return {"valid": workout.valid, "steps": describe_steps(workout) if workout.steps else [], "errors": list(workout.errors),
            "name": workout.name, "day": workout.day.isoformat() if workout.day else None}


def new_workout(config: Config, kind: str, day: date) -> Path:
    """A file from the template; a second one for the same day and kind gets a numbered name."""
    kind = kind if kind in TEMPLATE_KINDS else "time"
    workout = template(kind, day, config.workout.defaults())
    base = workout.name
    for attempt in range(2, 12):
        try:
            return save(config, workout)
        except FileExistsError:
            workout.path = None
            workout.name = f"{base[: MAX_NAME_LEN - 3]} {attempt}"
    raise FileExistsError("no free file name for a new workout")
