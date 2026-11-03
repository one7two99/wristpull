"""What each dashboard page shows, assembled from library functions (no SQL here).

The functions return plain dicts and lists; the templates format and translate them, and the
chart series go to ECharts as JSON (missing values stay ``None`` -> ``null``, drawn as gaps).
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

from ..aggregate import daily_frame, weekly_frame
from ..coach.metrics import acute_chronic, deltas, recovery_flags, value
from ..coach.store import recent_runs
from ..status import last_imports
from ..tui.queries import day_rows

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
