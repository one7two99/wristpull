"""What each dashboard page shows, assembled from library functions (no SQL here).

The functions return plain dicts and lists; the templates format and translate them, and the
chart series go to ECharts as JSON (missing values stay ``None`` -> ``null``, drawn as gaps).
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..aggregate import daily_frame, weekly_frame
from ..coach.metrics import acute_chronic, deltas, recovery_flags, value
from ..coach.store import recent_notes, recent_runs
from ..config import Config
from .. import series
from ..aggregate import month_bounds, week_bounds
from ..detail import activity_detail, day_detail_rows, downsample, local_clock, night_samples, sleep_night, sports, stage_baseline
from ..export import ExportResult
from ..jobtext import last_days
from ..report import ReportResult
from ..tui.queries import inspect_fit, raw_file_info
from . import files as filesafe
from ..merge import STRAP_PRIORITY, merged_activities, merged_sleep
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
TREND_WEEKS = (4, 12, 52, 104, 260, 520)  # beyond a year the trend charts aggregate per week
# raw_files.source -> label msgid shown as "last sync"
SOURCE_LABELS = {"garmin_usb": "Watch (USB)", "gadgetbridge_garmin": "Watch (phone)", "garmin_connect": "Connect export", "zepp_ble": "Strap (Bluetooth)", "gadgetbridge_zepp": "Strap (phone)", "gadgetbridge": "Strap (phone)"}
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


def _spark(days: list[str], values: list[float | None], *, last: int = 14) -> dict[str, Any]:
    return {"labels": days[-last:], "data": values[-last:]}


def today_data(conn: sqlite3.Connection, today: date, tz: ZoneInfo | None = None) -> dict[str, Any]:
    """Tiles against the 28-day baseline (with a 14-day sparkline and a link to the metric page), recovery
    flags, load, today's Body Battery and stress curve, the latest coach check, last syncs."""
    rows = day_rows(conn, today - timedelta(days=60), today)
    tiles = []
    for metric, label, unit, kind, good in TILES:
        d = deltas(rows, metric)
        scale = 3600 if metric == "sleep_duration_s" else 1
        tolerance = 0.25 if metric == "sleep_duration_s" else (200 if metric == "steps" else 1)
        delta = d["delta_28d"] / scale if d["delta_28d"] is not None else None
        key = series.TILE_KEYS[metric]
        recent = series.metric_series(conn, key, today - timedelta(days=13), today)
        tiles.append(
            {
                "metric": metric,
                "page": key,
                "label": label,
                "unit": unit,
                "kind": kind,
                "value": d["latest"] / scale if d["latest"] is not None else None,
                "mean_28d": d["mean_28d"] / scale if d["mean_28d"] is not None else None,
                "delta_28d": delta,
                "tone": tone(delta, good, tolerance=tolerance),
                "n_28d": d["n_28d"],
                "spark": {"id": f"spark-{key}", "kind": "sparkline", "title": label, "unit": unit, "labels": recent["days"],
                          "series": [{"name": label, "data": series.merged(recent["garmin"], recent["strap"])}]},
            }
        )
    load = acute_chronic(rows) if rows else {}
    runs = recent_runs(conn, kind="daily", limit=1)
    coach = None
    if runs:
        run = runs[0]
        text = (run.get("answer_md") or "").strip()
        excerpt = text[:COACH_EXCERPT_CHARS] + ("…" if len(text) > COACH_EXCERPT_CHARS else "")
        coach = {
            "id": run["id"],
            "created_at_utc": run.get("created_at_utc"),
            "backend": run.get("backend"),
            "model": run.get("model"),
            "rating": run.get("rating"),
            "excerpt": excerpt,
            "blocks": coach_blocks(excerpt),
        }
    curve = None
    if tz is not None:
        curve = day_curve(conn, today, tz)
    return {
        "as_of": rows[-1]["date"] if rows else None,
        "tiles": tiles,
        "sleep_score": value(rows[-1], "sleep_score") if rows else None,
        "flags": recovery_flags(rows, load) if rows else [],
        "load": load,
        "coach": coach,
        "imports": last_imports(conn),
        "curve": curve,
    }


def day_curve(conn: sqlite3.Connection, day: date, tz: ZoneInfo) -> dict[str, Any] | None:
    """Body Battery and stress of one day on the same 0–100 scale (one chart, one axis), or None without samples."""
    bb = series.intraday(conn, "body_battery", day, tz) or {"series": [], "nights": []}
    st = series.intraday(conn, "stress", day, tz) or {"series": [], "nights": []}
    picked = []
    for name, got in (("Body Battery", bb), ("Stress", st)):
        watch = next((s for s in got["series"] if s["device"] == "garmin"), None)
        if watch:
            picked.append({"name": name, "points": watch["points"]})
    if not picked:
        return None
    return {"id": "day-curve", "kind": "intraday", "title": "Body Battery and stress today", "unit": "", "labels": [], "series": picked,
            "nights": bb["nights"] or st["nights"], "range": [0, 100]}


def acwr_series(rows: list[dict[str, Any]], days: list[str]) -> list[float | None]:
    """ACWR as of each day (only once 28 days of history exist, like the coach)."""
    out: list[float | None] = []
    for day in days:
        upto = [r for r in rows if r["date"] <= day]
        load = acute_chronic(upto) if upto else {}
        out.append(load.get("acwr") if load.get("sufficient_history") else None)
    return out


week_labels = series.week_labels


def weekly_points(days: list[str], values: list[float | None], agg: str = "mean", digits: int = 1) -> list[float | None]:
    """Fold daily values into one per ISO week (see ``series.fold_weekly``)."""
    return series.fold_weekly(days, values, agg, digits)


def _window(today: date, weeks: int) -> tuple[date, int, bool]:
    weeks = weeks if weeks in TREND_WEEKS else TREND_WEEKS[0]
    return today - timedelta(days=7 * weeks - 1), weeks, weeks > 52


def trends_data(conn: sqlite3.Connection, today: date, weeks: int) -> dict[str, Any]:
    """One tile per metric (latest value, delta against the 28-day mean, sparkline over the period, link to the
    metric page) plus the training-load charts (weekly hours, ACWR); beyond a year the series fold per ISO week."""
    start, weeks, by_week = _window(today, weeks)
    rows = day_rows(conn, start - timedelta(days=35), today)  # history before the window feeds the ACWR
    by_day = {r["date"]: r for r in rows}
    days = [(start + timedelta(days=i)).isoformat() for i in range((today - start).days + 1)]
    labels = series.week_labels(days) if by_week else days
    shared = {d: by_day[d] for d in days if d in by_day}
    tiles = []
    for m in series.METRICS:
        got = series.metric_series(conn, m.key, start, today, rows=shared)
        values = series.merged(got["garmin"], got["strap"])
        if m.metric and not any(v is not None for v in values):
            continue  # a scale or VO2max estimate the user never had: no empty tile
        st = series.stats(days, values, digits=max(1, m.digits))
        delta = st["latest"] - st["mean_28d"] if st["latest"] is not None and st["mean_28d"] is not None else None
        folded = series.fold_weekly(days, values, m.week_agg, max(1, m.digits)) if by_week else values
        tiles.append({
            "key": m.key, "title": m.title, "unit": m.unit, "kind": m.kind, "digits": m.digits,
            "latest": st["latest"], "latest_date": st["latest_date"], "mean_28d": st["mean_28d"],
            "delta": round(delta, max(1, m.digits)) if delta is not None else None,
            "tone": tone(delta, m.good, tolerance=m.tolerance),
            "n": st["n"],
            "chart": {"id": f"spark-{m.key}", "kind": "sparkline", "title": m.title, "unit": m.unit, "labels": labels,
                      "series": [{"name": m.title, "data": folded}]},
        })
    weekly = weekly_frame(daily_frame(conn, start.isoformat(), today.isoformat()))
    week_rows = weekly.to_dicts() if not weekly.is_empty() else []
    history_days = [r["date"] for r in rows]
    all_days = [(history_days[0] if history_days and history_days[0] < days[0] else days[0])]
    all_days = [(date.fromisoformat(all_days[0]) + timedelta(days=i)).isoformat() for i in range((today - date.fromisoformat(all_days[0])).days + 1)]
    loads = [by_day[d].get("garmin_training_load") if d in by_day else None for d in all_days]
    acwr = series.rolling_acwr(all_days, loads, present=[d in by_day for d in all_days])[-len(days) :]
    charts = [
        {"id": "volume", "title": "Training per week", "unit": "h", "kind": "bar", "labels": [str(w["week"]) for w in week_rows],
         "series": [{"name": "Hours", "data": [_r(w.get("training_h"), 2) for w in week_rows]}]},
        {"id": "acwr", "title": "Acute:chronic load", "unit": "", "kind": "acwr", "labels": labels,
         "series": [{"name": "ACWR", "data": series.fold_weekly(days, acwr, "mean", 2) if by_week else acwr}], "band": [0.8, 1.3]},
    ]
    return {"weeks": weeks, "start": start.isoformat(), "end": today.isoformat(), "days_with_data": sum(1 for d in days if d in by_day), "tiles": tiles, "charts": charts}


# ---- metric pages ------------------------------------------------------------------------

COMPARE_NONE = "none"


def _main_chart(m: series.MetricSpec, days: list[str], got: dict[str, Any], *, by_week: bool, chart_id: str = "main") -> dict[str, Any]:
    """The daily series of a metric as a chart definition: watch and strap, the 28-day band and the 7-day mean."""
    digits = max(1, m.digits)
    values = series.merged(got["garmin"], got["strap"])
    means, stds = series.rolling_baseline(values)
    band_low = [round(a - b, digits) if a is not None and b is not None else None for a, b in zip(means, stds)]
    band_high = [round(a + b, digits) if a is not None and b is not None else None for a, b in zip(means, stds)]
    mean7 = series.smooth(values, digits=digits)
    chart_series = [{"name": "Watch", "data": got["garmin"], "role": "watch"}]
    if got["strap"] is not None and any(v is not None for v in got["strap"]):
        chart_series.append({"name": "Strap", "data": got["strap"], "role": "strap"})
    if m.kind != "points":  # the 7-day mean and the 28-day band are chrome, drawn below the data series
        chart_series.append({"name": "7-day mean", "data": mean7, "role": "mean"})
        chart_series.append({"name": "28-day band", "data": band_low, "role": "band_low"})
        chart_series.append({"name": "28-day band", "data": band_high, "role": "band_high"})
    if by_week:
        labels = series.week_labels(days)
        for s in chart_series:
            s["data"] = series.fold_weekly(days, s["data"], m.week_agg if s["role"] in ("watch", "strap") else "mean", digits)
    else:
        labels = days
    return {"id": chart_id, "title": m.title, "unit": m.unit, "kind": m.kind, "labels": labels, "series": chart_series, "zoom": True, "weekly": by_week}


def metric_page(conn: sqlite3.Connection, key: str, today: date, weeks: int, *, day: str | None, compare: str | None, tz: ZoneInfo) -> dict[str, Any] | None:
    """Everything the metric page shows; None for an unknown key."""
    m = series.spec(key)
    if m is None:
        return None
    start, weeks, by_week = _window(today, weeks)
    history = start - timedelta(days=series.BASELINE_DAYS)  # the band needs the 28 days before the window
    days = [(history + timedelta(days=i)).isoformat() for i in range((today - history).days + 1)]
    got = series.metric_series(conn, key, history, today)
    values = series.merged(got["garmin"], got["strap"])
    offset = (start - history).days
    window_days, window_values = days[offset:], values[offset:]
    chart = _main_chart(m, days, got, by_week=by_week)  # built over the history so the band exists from day one of the window
    keep = len(series.week_labels(window_days)) if by_week else len(window_days)
    chart["labels"] = chart["labels"][-keep:]
    for s in chart["series"]:
        s["data"] = s["data"][-keep:]
    st = series.stats(days, values, digits=max(1, m.digits))
    compare_key = compare if compare in series.BY_KEY and compare != key else (None if compare == COMPARE_NONE else m.compare)
    compare_chart = None
    if compare_key:
        other = series.spec(compare_key)
        other_got = series.metric_series(conn, compare_key, start, today)
        compare_chart = _main_chart(other, window_days, other_got, by_week=by_week, chart_id="compare")
        compare_chart["series"] = [s for s in compare_chart["series"] if s["role"] in ("watch", "strap")]
        compare_chart["zoom"] = False
    sample_days = [d for d, v in zip(window_days, window_values) if v is not None] if m.samples else []
    chosen = day if day in sample_days else (sample_days[-1] if sample_days else None)
    curve = intraday_chart(conn, key, date.fromisoformat(chosen), tz, sample_days) if chosen else None
    weekday = series.weekday_pattern(window_days, window_values, digits=max(1, m.digits))
    deviation = m.kind == "line"  # a level (HR, HRV, score): show each weekday's difference from the period mean, not bars from zero
    present_values = [v for v in window_values if v is not None]
    overall = sum(present_values) / len(present_values) if present_values else None
    weekday_data = [round(w["mean"] - overall, max(1, m.digits)) if deviation and w["mean"] is not None and overall is not None else w["mean"] for w in weekday]
    table = [
        {"date": d, "garmin": g, "strap": s, "mean": round(mu, max(1, m.digits)) if mu is not None else None}
        for d, g, s, mu in zip(reversed(window_days), reversed(got["garmin"][offset:]), reversed(got["strap"][offset:] if got["strap"] else [None] * len(window_days)), reversed(series.rolling_baseline(values)[0][offset:]))
        if g is not None or s is not None
    ]
    return {
        "m": m,
        "weeks": weeks,
        "start": start.isoformat(),
        "end": today.isoformat(),
        "weekly": by_week,
        "stats": st,
        "chart": chart,
        "compare": compare_key,
        "compare_chart": compare_chart,
        "compare_options": [o for o in series.METRICS if o.key != key],
        "curve": curve,
        "weekday": {"id": "weekday", "title": "Weekday pattern (difference from the period mean)" if deviation else "Weekday pattern", "unit": m.unit, "kind": "bar",
                    "labels": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], "series": [{"name": m.title, "data": weekday_data}], "counts": [w["n"] for w in weekday], "deviation": deviation},
        "table": table,
        "has_strap": got["strap"] is not None and any(v is not None for v in got["strap"]),
    }


def intraday_chart(conn: sqlite3.Connection, key: str, day: date, tz: ZoneInfo, sample_days: list[str] | None = None) -> dict[str, Any]:
    """The day's curve as a chart definition plus the neighbouring days that have samples (for the ← → links)."""
    m = series.BY_KEY[key]
    got = series.intraday(conn, key, day, tz) or {"series": [], "nights": []}
    chart_series = [{"name": DEVICE_LABELS.get(s["device"], s["device"]), "points": s["points"]} for s in got["series"]]
    stamp = day.isoformat()
    prev_day = next_day = None
    if sample_days:
        earlier = [d for d in sample_days if d < stamp]
        later = [d for d in sample_days if d > stamp]
        prev_day, next_day = (earlier[-1] if earlier else None), (later[0] if later else None)
    rng = [0, 100] if m.samples in ("stress", "body_battery") else ([80, 100] if m.samples == "spo2" else None)
    return {"id": "curve", "kind": "intraday", "title": m.title, "unit": m.unit, "labels": [], "series": chart_series, "nights": got["nights"],
            "range": rng, "stress": m.samples == "stress", "step": m.samples in series.CUMULATIVE, "day": stamp, "prev": prev_day, "next": next_day, "metric": key}


# ---- activities and sleep (wave D2) ---------------------------------------------------------

DEVICE_LABELS = {"garmin": "Watch", "strap": "Strap"}
STAGE_ORDER = ("deep", "light", "rem", "awake", "unmeasurable")
CURVE_POINTS = 400


def _period(today: date, weeks: int) -> tuple[date, int]:
    weeks = weeks if weeks in TREND_WEEKS else TREND_WEEKS[0]
    return today - timedelta(days=7 * weeks - 1), weeks


def activity_title(row: dict[str, Any]) -> str:
    name = (row.get("name") or row.get("profile_name") or "").strip()
    sport = (row.get("sport") or "activity").replace("_", " ")
    return name if name and name.lower() != sport.lower() else sport.capitalize()


def activities_list(conn: sqlite3.Connection, today: date, weeks: int, *, sport: str | None = None, q: str | None = None, tz: ZoneInfo) -> dict[str, Any]:
    """Merged activities (Garmin USB/phone duplicates removed, strap separate), newest first; ``q`` filters by name."""
    start, weeks = _period(today, weeks)
    rows = list(reversed(merged_activities(conn, start.isoformat(), today.isoformat())))
    if sport:
        rows = [r for r in rows if (r.get("sport") or "") == sport]
    needle = (q or "").strip().lower()
    if needle:
        rows = [r for r in rows if needle in f"{r.get('name') or ''} {r.get('profile_name') or ''} {r.get('sport') or ''}".lower()]
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
        "q": needle,
        "sports": sports(conn),
        "items": items,
        "total_s": sum(i["duration_s"] or 0 for i in items),
        "total_m": sum(i["distance_m"] or 0 for i in items),
        "weekly": weekly_sport_chart(items, start, today),
    }


def weekly_sport_chart(items: list[dict[str, Any]], start: date, end: date) -> dict[str, Any] | None:
    """Hours per ISO week, stacked by sport (the three biggest sports, the rest as "Other")."""
    if not items:
        return None
    mondays = series.week_labels([(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)])
    totals: dict[str, float] = {}
    for a in items:
        totals[a["sport"] or "other"] = totals.get(a["sport"] or "other", 0.0) + (a["duration_s"] or 0)
    top = [k for k, _v in sorted(totals.items(), key=lambda kv: -kv[1])[:MAX_SPORT_SERIES]]
    names = top + (["Other"] if len(totals) > len(top) else [])
    hours = {name: [0.0] * len(mondays) for name in names}
    index = {m: i for i, m in enumerate(mondays)}
    for a in items:
        day = date.fromisoformat(a["date"])
        monday = (day - timedelta(days=day.weekday())).isoformat()
        if monday not in index:
            continue
        key = a["sport"] or "other"
        hours[key if key in top else "Other"][index[monday]] += (a["duration_s"] or 0) / 3600
    return {"id": "weekly", "title": "Hours per week", "unit": "h", "kind": "stacked", "labels": mondays,
            "series": [{"name": name if name == "Other" else name.replace("_", " "), "data": [round(v, 2) if v else None for v in hours[name]]} for name in names]}


PACE_SPORTS = ("running", "walking", "hiking", "trail_running")
TRACK_POINTS = 600
MAX_SPORT_SERIES = 3


def _pace_min_per_km(speed_mps: float | None) -> float | None:
    """min/km as a decimal number (5.5 = 5:30); None below walking speed, so stops stay gaps."""
    if speed_mps is None or speed_mps < 0.5:
        return None
    return round(1000 / speed_mps / 60, 2)


def activity_page(conn: sqlite3.Connection, activity_id: int, tz: ZoneInfo) -> dict[str, Any] | None:
    """One activity: key numbers, linked curves (HR, pace or speed, elevation, cadence, power) over elapsed
    minutes, the zone donut, the lap chart and the track drawn from the GPS points (no map)."""
    detail = activity_detail(conn, activity_id)
    if detail is None:
        return None
    records = detail.pop("records")
    t0 = parse_iso_utc(records[0]["ts_utc"]) if records else None
    minutes = [round((parse_iso_utc(r["ts_utc"]) - t0).total_seconds() / 60, 1) for r in records] if t0 else []
    labels = [str(round(m)) if m is not None else "" for m in downsample(minutes, CURVE_POINTS)]
    sport = detail.get("sport") or ""
    pace_sport = sport in PACE_SPORTS
    curves: list[tuple[str, str, str, list[float | None], int, str | None]] = [  # (id, title, unit, values, digits, format)
        ("hr", "Heart rate", "bpm", [r["hr"] for r in records], 0, None),
        ("pace", "Pace", "min/km", [_pace_min_per_km(r["speed_mps"]) for r in records], 2, "pace") if pace_sport
        else ("speed", "Speed", "km/h", [round(r["speed_mps"] * 3.6, 1) if r["speed_mps"] is not None else None for r in records], 1, None),
        ("alt", "Altitude", "m", [r["altitude_m"] for r in records], 0, None),
        ("cadence", "Cadence", "rpm" if sport == "cycling" else "spm", [r["cadence"] for r in records], 0, None),
        ("power", "Power", "W", [r["power_w"] for r in records], 0, None),
    ]
    charts = []
    for chart_id, title, unit, values, digits, fmt in curves:
        data = downsample(values, CURVE_POINTS)
        if not any(v is not None for v in data):
            continue
        chart = {"id": chart_id, "title": title, "unit": unit, "kind": "line", "labels": labels, "group": "activity", "format": fmt,
                 "series": [{"name": title, "data": [_r(v, digits) for v in data]}]}
        if chart_id == "alt":
            chart["area"] = True
        if fmt == "pace":
            chart["inverse"] = True  # faster is up
        charts.append(chart)
    zones_total = sum(detail["hr_zones_s"]) or 0
    # FIT time_in_hr_zone: entry 0 is the time below zone 1, then zones 1..n
    zones = [{"label": "<Z1" if i == 0 else f"Z{i}", "seconds": s, "pct": round(100 * s / zones_total) if zones_total else 0, "zone": i}
             for i, s in enumerate(detail["hr_zones_s"])] if zones_total else []
    donut = {"id": "zones", "title": "Time in heart-rate zones", "unit": "", "kind": "donut", "labels": [z["label"] for z in zones],
             "series": [{"name": "Time in heart-rate zones", "data": [round(z["seconds"] / 60, 1) for z in zones]}], "zones": True} if zones else None
    laps = detail["laps"]
    lap_chart = None
    if len(laps) > 1:
        if pace_sport and all(lap.get("distance_m") for lap in laps):
            values = [_pace_min_per_km((lap["distance_m"] or 0) / (lap.get("timer_s") or lap.get("elapsed_s") or 1)) for lap in laps]
            lap_chart = {"id": "laps", "title": "Pace per lap", "unit": "min/km", "kind": "bar", "format": "pace"}  # shorter bar = faster lap
        else:
            values = [_r((lap.get("timer_s") or lap.get("elapsed_s") or 0) / 60, 1) for lap in laps]
            lap_chart = {"id": "laps", "title": "Minutes per lap", "unit": "min", "kind": "bar"}
        lap_chart.update({"labels": [str(lap["lap_index"] + 1) for lap in laps], "series": [{"name": lap_chart["title"], "data": values}]})
    points = [(r["lon"], r["lat"]) for r in records if r.get("lat") is not None and r.get("lon") is not None]
    track = None
    if len(points) >= 2:
        step = max(1, len(points) // TRACK_POINTS)
        thinned = points[::step] + ([points[-1]] if (len(points) - 1) % step else [])
        track = {"id": "track", "title": "Track", "unit": "", "kind": "track", "labels": [], "series": [{"name": "Track", "data": [[round(lon, 6), round(lat, 6)] for lon, lat in thinned]}]}
    archive = detail.get("archive_path")
    return {
        "a": detail,
        "title": activity_title(detail),
        "download": f"raw/{archive}" if archive else None,  # relative to the data directory, for /files/download
        "clock": local_clock(detail["start_utc"], tz),
        "duration_s": detail.get("timer_s") or detail.get("elapsed_s"),
        "pace_sport": pace_sport,
        "charts": charts,
        "zones": zones,
        "donut": donut,
        "lap_chart": lap_chart,
        "track": track,
        "laps": laps,
    }


SLEEP_SOURCES = ("all", "watch", "strap")


def sleep_list(conn: sqlite3.Connection, today: date, weeks: int, *, source: str = "all", tz: ZoneInfo) -> dict[str, Any]:
    """One row per wake-up date with the watch's and the strap's night side by side, newest first;
    ``source`` watch|strap keeps only the nights of that device."""
    start, weeks = _period(today, weeks)
    source = source if source in SLEEP_SOURCES else "all"
    by_day: dict[str, dict[str, Any]] = {}
    for s in merged_sleep(conn, start.isoformat(), today.isoformat()):
        day = by_day.setdefault(s["local_date"], {"date": s["local_date"], "watch": None, "strap": None})
        key = "watch" if s["device"] == "garmin" else "strap"
        if day[key] is None or (s.get("duration_s") or 0) > (day[key].get("duration_s") or 0):
            day[key] = s  # the main night if a device logged a nap too
    rows = sorted(by_day.values(), key=lambda d: d["date"], reverse=True)
    if source != "all":
        rows = [r for r in rows if r[source] is not None]
    for r in rows:
        for key in ("watch", "strap"):
            if r[key]:
                r[key]["bar"] = night_bar(r[key]["start_utc"], r[key]["end_utc"], tz)
                r[key]["tone"] = score_tone(r[key].get("score"))
    shown = [r[k] for r in rows for k in ("watch", "strap") if r[k] and (source == "all" or source == k)]
    durations = [n["duration_s"] for n in shown if n.get("duration_s")]
    scores = [n["score"] for n in shown if n.get("score") is not None]
    return {"weeks": weeks, "start": start.isoformat(), "rows": rows, "source": source, "sources": SLEEP_SOURCES,
            "mean_s": sum(durations) / len(durations) if durations else None, "mean_score": sum(scores) / len(scores) if scores else None, "n": len(shown)}


NIGHT_AXIS_START_MIN = 18 * 60  # the night axis runs from 18:00 to 12:00 the next day
NIGHT_AXIS_SPAN_MIN = 18 * 60
NIGHT_AXIS_TICKS = ("18:00", "21:00", "00:00", "03:00", "06:00", "09:00", "12:00")


def night_bar(start_utc: str, end_utc: str, tz: ZoneInfo) -> dict[str, float]:
    """Left and width (percent of an 18:00–12:00 axis) of one night, for the sleep list's bars."""
    a, b = parse_iso_utc(start_utc).astimezone(tz), parse_iso_utc(end_utc).astimezone(tz)
    bed = (a.hour * 60 + a.minute - NIGHT_AXIS_START_MIN) % (24 * 60)
    length = (b - a).total_seconds() / 60
    left = max(0.0, min(100.0, 100 * bed / NIGHT_AXIS_SPAN_MIN))
    width = max(0.5, min(100.0 - left, 100 * length / NIGHT_AXIS_SPAN_MIN))
    return {"left": round(left, 2), "width": round(width, 2)}


def score_tone(score: float | None) -> str:
    if score is None:
        return "none"
    return "good" if score >= 80 else ("mid" if score >= 60 else "low")


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


NIGHT_CURVES = (("heart_rate", "Heart rate", "bpm", 0), ("hrv", "HRV (RMSSD)", "ms", 0), ("respiration", "Respiration", "/min", 1), ("spo2", "SpO2", "%", 0))


def night_page(conn: sqlite3.Connection, sleep_id: int, tz: ZoneInfo) -> dict[str, Any] | None:
    """One night: hypnogram, the night curves on the same time axis, stage shares against the 28-day mean,
    totals, HRV/SpO2/respiration/HR, the other device's night."""
    night = sleep_night(conn, sleep_id)
    if night is None:
        return None
    same_day = merged_sleep(conn, night["local_date"], night["local_date"])
    this = next((s for s in same_day if s["id"] == sleep_id), None)
    device = this["device"] if this else ("strap" if night["source"] in STRAP_PRIORITY else "garmin")
    other = max((s for s in same_day if s["device"] != device), key=lambda s: s.get("duration_s") or 0, default=None)
    stage_totals = [{"stage": k, "seconds": night.get(f"{k}_s")} for k in STAGE_ORDER if night.get(f"{k}_s")]
    t0, t1 = parse_iso_utc(night["start_utc"]), parse_iso_utc(night["end_utc"])
    span_min = round((t1 - t0).total_seconds() / 60)
    bed_local = t0.astimezone(tz)
    x0 = bed_local.hour * 60 + bed_local.minute
    blocks = []
    for st in night["stages"]:
        if st["stage"] not in STAGE_ORDER[:4]:
            continue
        a = max(0.0, (parse_iso_utc(st["start_utc"]) - t0).total_seconds() / 60)
        b = min(float(span_min), (parse_iso_utc(st["end_utc"]) - t0).total_seconds() / 60)
        if b > a:
            blocks.append([round(a, 1), round(b, 1), STAGE_ORDER.index(st["stage"])])
    hypnogram = {"id": "hypnogram", "title": "Sleep stages", "unit": "", "kind": "hypnogram", "labels": [], "x0": x0, "xmax": span_min,
                 "stages": list(STAGE_ORDER[:4]), "series": [{"name": "Sleep stages", "data": blocks}], "group": "night"} if blocks else None
    samples = night_samples(conn, night["source"], night["start_utc"], night["end_utc"])
    curves = []
    for key, title, unit, digits in NIGHT_CURVES:
        points = samples.get(key)
        if not points:
            continue
        if len(points) > CURVE_POINTS:
            step = max(1, len(points) // CURVE_POINTS)
            points = points[::step]
        curves.append({"id": key, "title": title, "unit": unit, "kind": "timeline", "labels": [], "x0": x0, "xmax": span_min, "group": "night",
                       "series": [{"name": title, "points": [[p[0], round(p[1], digits)] for p in points]}]})
    baseline = stage_baseline(conn, night["source"], night["local_date"])
    shares = None
    duration = night.get("duration_s") or sum(t["seconds"] for t in stage_totals) or 0
    if duration and baseline["n"] and baseline["duration_s"]:
        labels = [k for k in STAGE_ORDER[:4]]
        this_pct = [round(100 * (night.get(f"{k}_s") or 0) / duration, 1) for k in labels]
        base_pct = [round(100 * (baseline.get(f"{k}_s") or 0) / baseline["duration_s"], 1) for k in labels]
        shares = {"id": "shares", "title": "Stage shares", "unit": "%", "kind": "bar", "labels": labels, "stage_labels": True,
                  "series": [{"name": "This night", "data": this_pct}, {"name": "28-day mean", "data": base_pct}], "baseline_nights": baseline["n"]}
    return {
        "n": night,
        "device": DEVICE_LABELS.get(device, device),
        "bed": local_clock(night["start_utc"], tz),
        "wake": local_clock(night["end_utc"], tz),
        "band": stage_band(night["stages"], night["start_utc"], night["end_utc"]),
        "stage_totals": stage_totals,
        "hypnogram": hypnogram,
        "curves": curves,
        "shares": shares,
        "charts": ([hypnogram] if hypnogram else []) + curves + ([shares] if shares else []),
        "other": other,
        "other_device": DEVICE_LABELS.get(other["device"], "") if other else None,
    }


# ---- coach, journal, workouts (wave D3) -------------------------------------------------------

COACH_KINDS = ("daily", "weekly", "ask")
TEMPLATE_KINDS = ("time", "distance", "intervals")


def coach_data(conn: sqlite3.Connection, tz: ZoneInfo, *, runs: int = 10, notes: int = 10, config: Config | None = None, lang: str = "en") -> dict[str, Any]:
    """Recent runs (answers, dry runs and failures, newest first) with tokens and cost, the goal profile's state,
    and the latest journal notes."""
    rows = recent_runs(conn, limit=runs) + recent_runs(conn, limit=5, status="dry_run") + recent_runs(conn, limit=5, status="error")
    items = []
    for run in sorted(rows, key=lambda r: r["id"], reverse=True)[:runs]:
        stamp = run.get("created_at_utc")
        tokens = None
        if run.get("input_tokens") is not None or run.get("output_tokens") is not None:
            tokens = f"{run.get('input_tokens') or '?'} / {run.get('output_tokens') or '?'}" + (" ~" if run.get("tokens_estimated") else "")
        items.append({
            "id": run["id"],
            "kind": run["kind"],
            "status": run.get("status") or "ok",
            "when": parse_iso_utc(stamp).astimezone(tz).strftime("%d.%m. %H:%M") if stamp else "–",
            "backend": run.get("backend"),
            "model": run.get("model"),
            "question": run.get("question"),
            "rating": run.get("rating"),
            "cost_usd": run.get("cost_usd"),
            "tokens": tokens,
            "duration_s": run.get("duration_s"),
            "error": run.get("error"),
            "answer_md": run.get("answer_md") or "",
            "blocks": coach_blocks(run.get("answer_md") or ""),
        })
    profile = None
    if config is not None:
        from ..coach.profile import load_profile, profile_path

        _text, _sha, placeholders = load_profile(config, lang)
        profile = {"path": str(profile_path(config)), "placeholders": placeholders}
    return {"runs": items, "notes": recent_notes(conn, notes), "profile": profile, "dryrun_dir": "coach/dryrun"}


def coach_profile_data(config: Config, lang: str) -> dict[str, Any]:
    """The goal profile's Markdown (the template on first use) and its open placeholders."""
    from ..coach.profile import load_profile, profile_path

    text, sha, placeholders = load_profile(config, lang)
    return {"text": text, "sha": sha, "placeholders": placeholders, "path": str(profile_path(config))}


def save_coach_profile(config: Config, text: str) -> list[str]:
    """Write the profile (normalised line endings, one trailing newline); returns the placeholders still open."""
    from ..coach.profile import profile_path
    from ..coach.prompts import unfilled_placeholders

    path = profile_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = text.replace("\r\n", "\n").strip() + "\n"
    path.write_text(body, "utf-8")
    return unfilled_placeholders(body)


def wizard_data(config: Config, lang: str) -> dict[str, Any]:
    """The wizard's questions in the viewer's language, prefilled with the answers of the last run."""
    from ..coach.profile import load_answers
    from ..coach.wizard import QUESTIONS

    lang = lang if lang in ("en", "de") else "en"
    previous = load_answers(config)
    questions = []
    for q in QUESTIONS:
        prev = previous.get(q.key)
        chosen = set(prev) if isinstance(prev, list) else ({prev} if prev else set())
        questions.append({
            "key": q.key,
            "text": q.text[lang],
            "multi": q.multi,
            "hint": q.hint.get(lang, ""),
            "options": [{"value": value, "label": label[lang], "checked": value in chosen} for value, label in q.options],
            "value": "" if q.options else (str(prev) if prev else ""),
        })
    return {"questions": questions, "lang": lang}


def wizard_answers(form: Any) -> dict[str, Any]:
    """The answers out of the posted form (multi questions as lists, empty answers left out)."""
    from ..coach.wizard import QUESTIONS

    answers: dict[str, Any] = {}
    for q in QUESTIONS:
        if q.options:
            allowed = {value for value, _label in q.options}
            if q.multi:
                chosen = [v for v in form.getlist(q.key) if v in allowed]
                if chosen:
                    answers[q.key] = chosen
            else:
                value = str(form.get(q.key) or "")
                if value in allowed:
                    answers[q.key] = value
        else:
            text = str(form.get(q.key) or "").strip()[:500]
            if text:
                answers[q.key] = text
    return answers


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


# ---- devices & sync (parity W1): what the device agent reports, the queue, the last jobs -----------

JOB_HISTORY = 20


def devices_data(conn: sqlite3.Connection, config: Config, *, now: datetime) -> dict[str, Any]:
    """Agent heartbeat and USB presence (``agent_state``), the strap's configuration, the current job."""
    from ..agent.queue import current, device_state
    from ..importers.strap import has_auth_key

    state = device_state(conn, now=now)
    job = current(conn)
    return {
        "agent": {
            "online": state["online"],
            "watch_present": state["watch_present"],
            "phone_present": state["phone_present"],
            "bluetooth_ready": state["bluetooth_ready"],
            "last_seen": state["last_seen_utc"],
            "version": state["version"],
        },
        "phone_serial": config.phone_serial or None,
        "strap_address": config.strap_address or None,
        "strap_has_key": has_auth_key() if config.strap_address else None,
        "job": job,
    }


def sync_data(conn: sqlite3.Connection, config: Config, *, now: datetime, tz: ZoneInfo) -> dict[str, Any]:
    """The sync page: devices plus the last jobs as rows."""
    from ..agent.queue import recent

    data = devices_data(conn, config, now=now)
    rows = []
    for job in recent(conn, JOB_HISTORY):
        when = parse_iso_utc(job.finished_utc or job.started_utc or job.requested_utc).astimezone(tz).strftime("%d.%m. %H:%M")
        summary = job.error if job.error else None
        rows.append({"id": job.id, "title": job.title, "status": job.status, "cli": job.cli, "when": when, "seconds": job.seconds(now) if job.finished_utc else 0, "summary": summary})
    data["jobs"] = rows
    return data


# ---- settings (parity W3): the config file, the secrets store, what .env still decides ------------

SECRET_FIELDS = (
    ("WRISTPULL_STRAP_AUTHKEY", "Band auth key", "32 hex digits, from Gadgetbridge's device settings or huami-token"),
    ("ANTHROPIC_API_KEY", "Anthropic API key", "for the Claude coach backend"),
    ("OPENROUTER_API_KEY", "OpenRouter API key", "for the OpenRouter coach backend"),
)
HOST_ENV = ("UID", "GID", "WRISTPULL_DATA", "WRISTPULL_HOST_DATA_DIR", "WRISTPULL_CONFIG", "WRISTPULL_SITE", "WRISTPULL_USB_GID")
PASSWORD_MIN = 8


def settings_data(config: Config, env: dict[str, str]) -> dict[str, Any]:
    """Everything the settings page shows: the config fields, which secrets are set and where, the host values."""
    from .. import secrets as store
    from ..config import COACH_BACKENDS, COACH_EFFORTS, UI_LANGUAGES, default_config_path

    c = config.coach
    return {
        "config_path": str(config.path or default_config_path(env)),
        "data_dir": str(config.file_data_dir or config.data_dir),
        "fields": {
            "timezone": config.timezone,
            "folders": ", ".join(config.garmin_folders),
            "phone_serial": config.phone_serial or "",
            "phone_export_dir": config.phone_export_dir,
            "strap_address": config.strap_address,
            "strap_adapter": config.strap_adapter,
            "strap_since_days": config.strap_since_days,
            "strap_keep_on_band": config.strap_keep_on_band,
            "agent_import_on_plug": config.agent_import_on_plug,
            "ui_language": config.ui_language or "",
            "ui_range_days": config.ui_range_days,
            "coach_backend": c.backend,
            "coach_model": c.model,
            "coach_weekly_model": c.weekly_model or "",
            "coach_api_key_env": c.api_key_env or "",
            "coach_ollama_url": c.ollama_url,
            "coach_num_ctx": c.num_ctx,
            "coach_max_tokens": c.max_tokens,
            "coach_timeout_s": f"{c.timeout_s:g}",
            "coach_token_budget": c.token_budget,
            "coach_effort": c.effort,
            "coach_language": c.language or "",
            "coach_sleep_target_h": f"{c.sleep_target_h:g}",
        },
        "backends": COACH_BACKENDS,
        "efforts": COACH_EFFORTS,
        "languages": UI_LANGUAGES,
        "secrets": [{"key": key, "label": label, "hint": hint, "source": store.source_of(key, config, env)} for key, label, hint in SECRET_FIELDS],
        "ollama_url": store.effective(store.OLLAMA_URL, config, env) or "",
        "ollama_source": store.source_of(store.OLLAMA_URL, config, env),
        "password_source": store.source_of(store.PASSWORD_HASH, config, env),
        "host": [{"key": key, "value": env.get(key, "")} for key in HOST_ENV],
        "store_path": str(store.path(config)),
    }


def _int_field(form: Any, name: str, default: int, problems: list[str]) -> int:
    raw = str(form.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        problems.append(f"{name} must be a whole number")
        return default


def config_from_form(config: Config, form: Any) -> tuple[Config, list[str]]:
    """A Config built from the settings form (not yet validated) plus the parse problems."""
    from dataclasses import replace

    problems: list[str] = []
    text = lambda name: str(form.get(name) or "").strip()  # noqa: E731
    flag = lambda name: form.get(name) is not None  # noqa: E731
    try:
        timeout = float(text("coach_timeout_s") or config.coach.timeout_s)
        sleep_target = float(text("coach_sleep_target_h") or config.coach.sleep_target_h)
    except ValueError:
        problems.append("coach timeout must be a number")
        timeout = config.coach.timeout_s
    coach = replace(
        config.coach,
        backend=text("coach_backend") or config.coach.backend,
        model=text("coach_model"),
        weekly_model=text("coach_weekly_model") or None,
        api_key_env=text("coach_api_key_env") or None,
        ollama_url=text("coach_ollama_url") or "http://localhost:11434",
        num_ctx=_int_field(form, "coach_num_ctx", config.coach.num_ctx, problems),
        max_tokens=_int_field(form, "coach_max_tokens", config.coach.max_tokens, problems),
        timeout_s=timeout,
        token_budget=_int_field(form, "coach_token_budget", config.coach.token_budget, problems),
        effort=text("coach_effort") or config.coach.effort,
        sleep_target_h=sleep_target,
        language=text("coach_language") or None,
    )
    folders = [f.strip() for f in text("folders").split(",") if f.strip()]
    cfg = replace(
        config,
        timezone=text("timezone") or config.timezone,
        garmin_folders=folders or list(config.garmin_folders),
        phone_serial=text("phone_serial") or None,
        phone_export_dir=text("phone_export_dir") or config.phone_export_dir,
        strap_address=text("strap_address").upper(),
        strap_adapter=text("strap_adapter"),
        strap_since_days=_int_field(form, "strap_since_days", config.strap_since_days, problems),
        strap_keep_on_band=flag("strap_keep_on_band"),
        agent_import_on_plug=flag("agent_import_on_plug"),
        ui_language=text("ui_language") or None,
        ui_range_days=_int_field(form, "ui_range_days", config.ui_range_days, problems),
        coach=coach,
    )
    return cfg, problems


def apply_config(target: Config, new: Config) -> None:
    """Copy every field of ``new`` into the running ``target`` (the app closes over one Config object)."""
    from dataclasses import fields

    for f in fields(Config):
        setattr(target, f.name, getattr(new, f.name))


# ---- days, files, export (parity wave P2) -------------------------------------------------------

DAY_SOURCE_LETTERS = {"garmin_usb": "G", "gadgetbridge_garmin": "P", "garmin_connect": "C", "zepp_ble": "b", "gadgetbridge_zepp": "s", "gadgetbridge": "s"}


def day_period(today: date, range_days: int, *, start: str | None, end: str | None, preset: str | None) -> tuple[date, date]:
    """The Days page's period: a preset (week, month, default), explicit from/to, or the default range."""
    if preset == "week":
        return week_bounds(today)
    if preset == "month":
        return month_bounds(today)
    try:
        if start and end:
            a, b = date.fromisoformat(start), date.fromisoformat(end)
            if a <= b and (b - a).days <= 366:
                return a, b
    except ValueError:
        pass
    return last_days(today, range_days)


def days_data(conn: sqlite3.Connection, start: date, end: date, *, day: str | None = None, lang: str | None = None) -> dict[str, Any]:
    """The TUI Days table: one row per day with data, the detail of ``day`` (default: the latest)."""
    rows = day_rows(conn, start, end)
    items = []
    for r in rows:
        mod, vig = r.get("garmin_moderate_min"), r.get("garmin_vigorous_min")
        items.append({
            "date": r["date"],
            "steps": r.get("garmin_steps"),
            "resting_hr": r.get("garmin_resting_hr"),
            "sleep_s": r.get("garmin_sleep_duration_s"),
            "bb_max": r.get("garmin_body_battery_max"),
            "bb_min": r.get("garmin_body_battery_min"),
            "stress": r.get("garmin_stress_avg"),
            "intensity": None if mod is None and vig is None else (mod or 0) + (vig or 0),
            "strap_steps": r.get("strap_steps"),
            "sources": "".join(DAY_SOURCE_LETTERS.get(src, src[:1]) for src in (r.get("sources") or "").split("+") if src),
        })
    chosen = day if day in {r["date"] for r in rows} else (rows[-1]["date"] if rows else None)
    span = (end - start).days + 1
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "rows": list(reversed(items)),
        "n": len(rows),
        "day": chosen,
        "detail": day_detail(rows, chosen, lang=lang) if chosen else None,
        "earlier": {"from": (start - timedelta(days=span)).isoformat(), "to": (start - timedelta(days=1)).isoformat()},
        "later": {"from": (end + timedelta(days=1)).isoformat(), "to": (end + timedelta(days=span)).isoformat()},
    }


def day_detail(rows: list[dict[str, Any]], day: str, *, lang: str | None = None) -> dict[str, Any] | None:
    row = next((r for r in rows if r["date"] == day), None)
    if row is None:
        return None
    return {"date": day, "cells": day_detail_rows(row, lang), "sources": row.get("sources") or "–"}


def files_data(conn: sqlite3.Connection, config: Config, rel: str | None, *, file: str | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
    """The listing of one folder of the data directory plus the selected file's info, archive row and inspect/preview."""
    from ..runtime import host_path

    data = {"listing": filesafe.listing(config.data_dir, rel), "root": str(host_path(config.data_dir, env)), "file": None}
    if file:
        path = filesafe.safe_path(config.data_dir, file)
        if not path.is_file():
            raise filesafe.BadPath(file)
        info = filesafe.file_info(path)
        info["rel"] = filesafe.relative(config.data_dir, path)
        info["host_path"] = str(host_path(path, env))
        row = raw_file_info(conn, config, path)
        info["archive"] = dict(row) if row else None
        info["inspect"] = None
        info["preview"] = None
        if info["kind"] == "fit":
            try:
                header, summary = inspect_fit(path)
                info["inspect"] = {"header": header, "messages": [{"name": name, "num": e["num"], "count": e["count"], "fields": list(e["fields"].keys())} for name, e in summary.items()]}
            except Exception as exc:  # noqa: BLE001 - a corrupt file is shown, not raised
                info["inspect"] = {"error": str(exc)}
        elif info["kind"] == "text":
            info["preview"] = filesafe.preview(path)
        data["file"] = info
    return data


EXPORT_KINDS = ("export", "week", "month")
EXPORT_TITLES = {"export": "Export for Claude", "week": "Weekly report", "month": "Monthly report"}
PREVIEW_LINES = 80


def export_form(today: date, range_days: int) -> dict[str, Any]:
    """Defaults and presets for the export form."""
    start, end = last_days(today, range_days)
    week = week_bounds(today)
    month = month_bounds(today)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "presets": [
            {"key": "7", "label": "7 days", "from": (today - timedelta(days=6)).isoformat(), "to": today.isoformat()},
            {"key": "default", "label": "default", "from": start.isoformat(), "to": end.isoformat()},
            {"key": "week", "label": "this week", "from": week[0].isoformat(), "to": week[1].isoformat()},
            {"key": "month", "label": "this month", "from": month[0].isoformat(), "to": month[1].isoformat()},
        ],
        "kinds": EXPORT_KINDS,
    }


def export_result(result: Any, config: Config, *, env: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Files (with sizes and download paths) and the Markdown preview of a finished export or report."""
    from ..runtime import host_path

    if not isinstance(result, (ExportResult, ReportResult)):
        return None
    files = []
    for path in result.files:
        p = Path(path)
        try:
            size = p.stat().st_size
        except OSError:
            size = None
        try:
            rel = filesafe.relative(config.data_dir, p)
        except ValueError:
            rel = None
        files.append({"name": p.name, "size": size, "rel": rel})
    try:
        directory_rel = filesafe.relative(config.data_dir, Path(result.directory))
    except ValueError:
        directory_rel = None
    lines = result.markdown.splitlines()
    return {
        "directory": str(host_path(Path(result.directory), env)),
        "directory_rel": directory_rel,
        "files": files,
        "preview": "\n".join(lines[:PREVIEW_LINES]),
        "truncated": len(lines) > PREVIEW_LINES,
        "lines": len(lines),
    }


def export_cli(kind: str, start: date, end: date, *, csv: bool, charts: bool) -> str:
    if kind == "export":
        return f"wristpull export --for-claude --from {start.isoformat()} --to {end.isoformat()}" + ("" if csv else " --no-csv")
    return f"wristpull report {kind} --date {end.isoformat()}" + (" --charts" if charts else "")


# ---- insights: readiness, load, sleep regularity, correlations, tags (analysis wave A3) --------------

INSIGHT_WEEKS = (4, 12, 26, 52)
TAG_WINDOW_DAYS = 7


def insights_data(conn: sqlite3.Connection, config: Config, today: date, weeks: int, *, lang: str = "en") -> dict[str, Any]:
    """The insights package plus the chart definitions the page draws from it."""
    from ..insights import build_insights
    from ..insights.tags import TAGS

    weeks = weeks if weeks in INSIGHT_WEEKS else INSIGHT_WEEKS[1]
    pkg = build_insights(conn, config, today, period_days=7 * weeks)
    dates = pkg["readiness"]["series"]["dates"]
    charts = [
        {"id": "readiness", "title": "Readiness", "unit": "", "kind": "line", "labels": dates, "range": [0, 100], "guides": [45, 70], "zoom": False,
         "series": [{"name": "Readiness", "data": pkg["readiness"]["series"]["score"]}]},
        {"id": "acwr", "title": "Acute:chronic load", "unit": "", "kind": "acwr", "labels": pkg["load"]["dates"], "band": [0.8, 1.3],
         "series": [{"name": "ACWR", "data": pkg["load"]["acwr"]}]},
        {"id": "nights", "title": "Bedtime and wake-up", "unit": "", "kind": "line", "format": "clock18", "labels": [n["date"] for n in pkg["sleep"]["nights"]], "zoom": False,
         "series": [{"name": "Bedtime", "data": [n["bed"] for n in pkg["sleep"]["nights"]]}, {"name": "Wake-up", "data": [(n["wake"] - 18 * 60) % 1440 for n in pkg["sleep"]["nights"]]}]},
    ]
    for i, c in enumerate(pkg["correlations"]):
        if c["label"] == "insufficient":
            continue
        charts.append({"id": f"corr-{i}", "title": c["y_title"], "unit": c["y_unit"], "kind": "scatter", "labels": [], "x_title": c["x_title"], "x_unit": c["x_unit"],
                       "x_format": "clock18" if c["x"] == "bed_offset" else None,
                       "series": [{"name": "Days", "points": c["points"]}, {"name": "Bin mean", "points": [[(b["lo"] if b["lo"] is not None else b["hi"]) if (b["lo"] is None or b["hi"] is None) else (b["lo"] + b["hi"]) / 2, b["mean_y"]] for b in c["bins"]]}],
                       "sweet": c["sweet_spot"]["bin"] if c["sweet_spot"] else None})
        c["chart_id"] = f"corr-{i}"
    latest = pkg["readiness"]["latest"]
    if latest:
        for comp in latest["components"]:
            comp["contribution_text"] = f"{comp['contribution']:+.1f}" if comp["contribution"] is not None else "–"
    tag_days = []
    for i in range(TAG_WINDOW_DAYS):
        day = (today - timedelta(days=i)).isoformat()
        tag_days.append({"date": day, "tags": pkg["tag_days"].get(day, [])})
    return {"weeks": weeks, "pkg": pkg, "charts": charts, "tags": TAGS, "tag_days": tag_days, "latest": latest}


def readiness_tile(conn: sqlite3.Connection, config: Config, today: date) -> dict[str, Any] | None:
    """The readiness of the latest day with a score and its three biggest contributions (for Today)."""
    from ..insights import build_insights

    pkg = build_insights(conn, config, today, period_days=14, pairs=())
    latest = pkg["readiness"]["latest"]
    if not latest:
        return None
    present = [c for c in latest["components"] if c["present"] and c["contribution"] is not None]
    reasons = sorted(present, key=lambda c: -abs(c["contribution"]))[:3]
    return {"date": latest["date"], "score": latest["score"], "label": latest["label"], "confidence": latest["confidence"],
            "reasons": [{"title": c["title"], "contribution": c["contribution"], "text": f"{c['contribution']:+.1f}"} for c in reasons]}
