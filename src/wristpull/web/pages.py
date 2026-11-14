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
from ..detail import activity_detail, downsample, local_clock, sleep_night, sports
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
    acwr = series.rolling_acwr(all_days, loads)[-len(days) :]
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
        "weekday": {"id": "weekday", "title": "Weekday pattern", "unit": m.unit, "kind": "bar", "labels": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                    "series": [{"name": m.title, "data": [w["mean"] for w in weekday]}], "counts": [w["n"] for w in weekday]},
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
    device = this["device"] if this else ("strap" if night["source"] in STRAP_PRIORITY else "garmin")
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
            "answer_md": run.get("answer_md") or "",
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
