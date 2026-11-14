"""Per-metric daily series, baselines and intraday curves for the dashboard's metric pages.

A metric is one column of ``daily_summary`` (or one sparse row kind of ``metrics``) with a title,
a unit, a chart kind and, for the sample-based metrics, the table the day's curve comes from.
The web layer calls these functions and renders; SQL is allowed here like in ``detail.py``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .aggregate import STRAP_METRICS, daily_frame
from .merge import GARMIN_PRIORITY, STRAP_PRIORITY, merged_metric_by_day
from .timeutil import parse_iso_utc

BASELINE_DAYS = 28
BASELINE_MIN = 7
SMOOTH_DAYS = 7
INTRADAY_STEP_MIN = 5  # the day's curve is bucketed into 5-minute slots (288 points at most)
DAY_MIN = 24 * 60


@dataclass(frozen=True)
class MetricSpec:
    key: str
    title: str  # msgid
    unit: str  # msgid or ""
    column: str | None  # daily_summary column; None for a ``metrics`` row kind
    explain: str  # msgid, one sentence
    good: str | None = None  # "up" | "down" | None
    kind: str = "line"  # line | bar | points
    week_agg: str = "mean"  # mean | sum | last
    scale: float = 1.0  # stored value / scale = shown value
    digits: int = 0
    samples: str | None = None  # table of the intraday curve
    metric: str | None = None  # ``metrics.metric`` for sparse kinds
    compare: str | None = None  # default partner on the metric page
    tolerance: float = 1.0  # smaller deltas against the baseline count as neutral


METRICS: tuple[MetricSpec, ...] = (
    MetricSpec("resting_hr", "Resting HR", "bpm", "resting_hr", "The lowest sustained heart rate of the day; lower than your baseline usually means better recovery.", "down", samples="heart_rate", compare="hrv"),
    MetricSpec("hrv", "HRV last night", "ms", "hrv_last_night_avg_ms", "Heart-rate variability averaged over the night; higher than your baseline points to a rested nervous system.", "up", samples="hrv_values", compare="sleep_duration"),
    MetricSpec("sleep_duration", "Sleep", "h", "sleep_duration_s", "Time asleep in the main night, by wake-up date.", "up", kind="bar", scale=3600, digits=2, compare="hrv", tolerance=0.25),
    MetricSpec("sleep_score", "Sleep score", "", "sleep_score", "The watch's 0–100 rating of the night (duration, stages, restlessness).", "up", compare="sleep_duration"),
    MetricSpec("stress", "Stress", "", "stress_avg", "Daily average of the watch's 0–100 stress level (from heart-rate variability); lower is calmer.", "down", samples="stress", compare="body_battery"),
    MetricSpec("body_battery", "Body Battery", "", "body_battery_max", "The day's highest Body Battery, usually right after waking; it charges with sleep and rest and drains with stress and activity.", "up", samples="body_battery", compare="stress"),
    MetricSpec("steps", "Steps", "", "steps", "Steps per day as the watch counts them.", "up", kind="bar", samples="steps", compare="sleep_duration", tolerance=200),
    MetricSpec("intensity_minutes", "Intensity minutes", "min", "intensity_min", "Moderate plus twice the vigorous minutes, as the watch credits them; the WHO suggests 150 per week.", "up", kind="bar", week_agg="sum", compare="training_load", tolerance=5),
    MetricSpec("training_load", "Training load", "", "training_load", "Sum of the training load (EPOC-based) of the day's activities; the trend matters more than one day.", None, kind="bar", week_agg="sum", compare="hrv", tolerance=10),
    MetricSpec("spo2", "SpO2", "%", "spo2_avg", "Blood oxygen saturation averaged over the day's readings (mostly at night).", "up", digits=1, samples="spo2", compare="respiration", tolerance=0.5),
    MetricSpec("respiration", "Respiration", "/min", "respiration_avg", "Breaths per minute averaged over the day; a rise above your baseline can precede illness.", "down", digits=1, samples="respiration", compare="resting_hr", tolerance=0.5),
    MetricSpec("weight", "Weight", "kg", None, "Body weight from a scale or the Connect export; only the days with a reading.", None, kind="points", week_agg="last", digits=1, metric="weight_kg", compare="vo2max", tolerance=0.3),
    MetricSpec("vo2max", "VO2max", "ml/kg/min", None, "The watch's estimate of maximal oxygen uptake after runs and rides.", "up", kind="points", week_agg="last", digits=1, metric="vo2_max", compare="resting_hr", tolerance=0.5),
)
BY_KEY = {m.key: m for m in METRICS}
# daily_summary columns of the Today tiles -> metric page
TILE_KEYS = {"resting_hr": "resting_hr", "hrv_last_night_avg_ms": "hrv", "sleep_duration_s": "sleep_duration", "stress_avg": "stress", "steps": "steps", "body_battery_max": "body_battery"}
SAMPLE_SQL = {
    "heart_rate": "SELECT ts_utc, bpm AS v FROM heart_rate WHERE source = ? AND local_date = ? ORDER BY ts_utc",
    "stress": "SELECT ts_utc, level AS v FROM stress WHERE source = ? AND local_date = ? AND level >= 0 ORDER BY ts_utc",
    "body_battery": "SELECT ts_utc, level AS v FROM body_battery WHERE source = ? AND local_date = ? ORDER BY ts_utc",
    "hrv_values": "SELECT ts_utc, rmssd_ms AS v FROM hrv_values WHERE source = ? AND local_date = ? ORDER BY ts_utc",
    "spo2": "SELECT ts_utc, spo2_pct AS v FROM spo2 WHERE source = ? AND local_date = ? ORDER BY ts_utc",
    "respiration": "SELECT ts_utc, breaths_per_min AS v FROM respiration WHERE source = ? AND local_date = ? ORDER BY ts_utc",
    "steps": "SELECT ts_utc, SUM(steps) AS v FROM steps WHERE source = ? AND local_date = ? GROUP BY ts_utc ORDER BY ts_utc",
}
CUMULATIVE = {"steps"}
DEVICE_SOURCES = {"garmin": GARMIN_PRIORITY, "strap": STRAP_PRIORITY}


def spec(key: str) -> MetricSpec | None:
    return BY_KEY.get(key)


def _shown(v: Any, m: MetricSpec) -> float | None:
    return round(float(v) / m.scale, m.digits) if v is not None else None


def daily_rows(conn: sqlite3.Connection, start: date, end: date) -> dict[str, dict[str, Any]]:
    """``daily_frame`` as a dict by ISO date (pass it to ``metric_series`` when several metrics share the range)."""
    return {r["date"].isoformat(): r for r in daily_frame(conn, start.isoformat(), end.isoformat()).to_dicts()}


def metric_series(conn: sqlite3.Connection, key: str, start: date, end: date, *, rows: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """``days`` (every calendar day), ``garmin`` and ``strap`` values (None = no data); strap is None for metrics the band lacks."""
    m = BY_KEY[key]
    days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    if m.metric:
        values = merged_metric_by_day(conn, start.isoformat(), end.isoformat(), m.metric)
        return {"days": days, "garmin": [_shown(values.get(d), m) if d in values else None for d in days], "strap": None}
    if rows is None:
        rows = daily_rows(conn, start, end)

    def pick(prefix: str, d: str) -> float | None:
        row = rows.get(d)
        if row is None:
            return None
        if m.column == "intensity_min":
            mod, vig = row.get(f"{prefix}_moderate_min"), row.get(f"{prefix}_vigorous_min")
            return None if mod is None and vig is None else _shown((mod or 0) + 2 * (vig or 0), m)
        return _shown(row.get(f"{prefix}_{m.column}"), m)

    strap = [pick("strap", d) for d in days] if m.column in STRAP_METRICS else None
    return {"days": days, "garmin": [pick("garmin", d) for d in days], "strap": strap}


def merged(garmin: list[float | None], strap: list[float | None] | None) -> list[float | None]:
    """The watch's value, the strap's where the watch has none (the same fallback the coach uses)."""
    if strap is None:
        return list(garmin)
    return [g if g is not None else s for g, s in zip(garmin, strap)]


def rolling_baseline(values: list[float | None], window: int = BASELINE_DAYS, min_days: int = BASELINE_MIN) -> tuple[list[float | None], list[float | None]]:
    """Trailing mean and standard deviation of the ``window`` days before each day (the day itself excluded).

    Positions with fewer than ``min_days`` values in the window stay None, so the band only appears once
    there is a baseline to speak of; gaps in the input are skipped, not counted as zero. One implementation
    for the metric pages and the insights (``insights.baselines.rolling_stats``, polars)."""
    from .insights.baselines import rolling_stats

    return rolling_stats(values, window=window, min_days=min_days)


def smooth(values: list[float | None], window: int = SMOOTH_DAYS, digits: int = 1) -> list[float | None]:
    """Trailing mean over ``window`` days including the day (None where the day itself has no value)."""
    out: list[float | None] = []
    for i, v in enumerate(values):
        if v is None:
            out.append(None)
            continue
        chosen = [x for x in values[max(0, i - window + 1) : i + 1] if x is not None]
        out.append(round(sum(chosen) / len(chosen), digits))
    return out


def rolling_acwr(days: list[str], loads: list[float | None], *, present: list[bool] | None = None, acute: int = 7, chronic: int = 28, min_history: int = 28) -> list[float | None]:
    """Acute (7-day sum) to chronic (28-day mean × 7) training load per calendar day, None until ``min_history``
    days with data exist (``present``: days the watch recorded anything; default: days with a load); a day
    without activities counts as no load. Linear in the number of days."""
    out: list[float | None] = []
    values = [v or 0.0 for v in loads]
    seen = 0
    acute_sum = chronic_sum = 0.0
    for i, v in enumerate(values):
        if (present[i] if present is not None else loads[i] is not None):
            seen += 1
        acute_sum += v
        chronic_sum += v
        if i >= acute:
            acute_sum -= values[i - acute]
        if i >= chronic:
            chronic_sum -= values[i - chronic]
        window = min(i + 1, chronic)
        chronic_weekly = chronic_sum / window * acute
        out.append(round(acute_sum / chronic_weekly, 2) if seen >= min_history and chronic_weekly > 0 else None)
    return out


def z_score(value: float | None, mean: float | None, std: float | None) -> float | None:
    if value is None or mean is None or std is None or std < 1e-9:
        return None
    return round((value - mean) / std, 2)


def stats(days: list[str], values: list[float | None], *, digits: int = 1) -> dict[str, Any]:
    """Latest value, 7- and 28-day means (before the latest day), extremes with their dates, the latest z-score."""
    present = [(d, v) for d, v in zip(days, values) if v is not None]
    if not present:
        return {"latest": None, "latest_date": None, "mean_7d": None, "mean_28d": None, "min": None, "min_date": None, "max": None, "max_date": None, "z": None, "n": 0}
    latest_date, latest = present[-1]
    earlier = [v for _d, v in present[:-1]]
    m7 = sum(earlier[-7:]) / len(earlier[-7:]) if earlier else None
    m28 = sum(earlier[-28:]) / len(earlier[-28:]) if earlier else None
    means, stds = rolling_baseline(values)
    idx = days.index(latest_date)
    lo = min(present, key=lambda p: p[1])
    hi = max(present, key=lambda p: p[1])
    return {
        "latest": latest,
        "latest_date": latest_date,
        "mean_7d": round(m7, digits) if m7 is not None else None,
        "mean_28d": round(m28, digits) if m28 is not None else None,
        "min": lo[1],
        "min_date": lo[0],
        "max": hi[1],
        "max_date": hi[0],
        "z": z_score(latest, means[idx], stds[idx]),
        "n": len(present),
    }


def weekday_pattern(days: list[str], values: list[float | None], *, digits: int = 1) -> list[dict[str, Any]]:
    """Mean per weekday (Monday first) over the days with a value."""
    buckets: list[list[float]] = [[] for _ in range(7)]
    for d, v in zip(days, values):
        if v is not None:
            buckets[date.fromisoformat(d).weekday()].append(v)
    return [{"weekday": i, "mean": round(sum(b) / len(b), digits) if b else None, "n": len(b)} for i, b in enumerate(buckets)]


def week_labels(days: list[str]) -> list[str]:
    """The Monday of every ISO week touched by ``days`` (the x axis beyond a year)."""
    labels: list[str] = []
    for d in days:
        monday = (date.fromisoformat(d) - timedelta(days=date.fromisoformat(d).weekday())).isoformat()
        if not labels or labels[-1] != monday:
            labels.append(monday)
    return labels


def fold_weekly(days: list[str], values: list[float | None], agg: str = "mean", digits: int = 1) -> list[float | None]:
    """One value per ISO week: the mean (or ``sum``) of the days with a value, or the ``last`` one."""
    buckets: dict[str, list[float]] = {}
    order: list[str] = []
    for d, v in zip(days, values):
        monday = (date.fromisoformat(d) - timedelta(days=date.fromisoformat(d).weekday())).isoformat()
        if monday not in buckets:
            buckets[monday] = []
            order.append(monday)
        if v is not None:
            buckets[monday].append(float(v))
    out: list[float | None] = []
    for monday in order:
        vals = buckets[monday]
        if not vals:
            out.append(None)
        elif agg == "last":
            out.append(round(vals[-1], digits))
        elif agg == "sum":
            out.append(round(sum(vals), digits))
        else:
            out.append(round(sum(vals) / len(vals), digits))
    return out


# ---- the day's curve ---------------------------------------------------------------------


def _minute_of_day(ts_utc: str, day: date, tz: ZoneInfo) -> float:
    local = parse_iso_utc(ts_utc).astimezone(tz)
    midnight = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    return (local - midnight).total_seconds() / 60


def bucket_points(points: list[tuple[float, float]], step: float = INTRADAY_STEP_MIN, *, cumulative: bool = False, digits: int = 1) -> list[list[float]]:
    """Average (or, for counters, the maximum of) the samples in each ``step``-minute slot: [[minute, value], ...]."""
    slots: dict[int, list[float]] = {}
    for minute, value in points:
        slots.setdefault(int(minute // step), []).append(value)
    out = []
    for slot in sorted(slots):
        vals = slots[slot]
        v = max(vals) if cumulative else sum(vals) / len(vals)
        out.append([slot * step, round(v, digits)])
    return out


def _first_source_with_samples(conn: sqlite3.Connection, table: str, sources: tuple[str, ...], day: str) -> str | None:
    for source in sources:
        if conn.execute(f"SELECT 1 FROM {table} WHERE source = ? AND local_date = ? LIMIT 1", (source, day)).fetchone():  # noqa: S608 (table from SAMPLE_SQL keys)
            return source
    return None


def intraday(conn: sqlite3.Connection, key: str, day: date, tz: ZoneInfo) -> dict[str, Any] | None:
    """The day's samples per device as [minute of the local day, value] points, plus the night windows.

    Returns None for metrics without a sample table; a dict with empty ``series`` when the day has no samples."""
    m = BY_KEY[key]
    if not m.samples:
        return None
    sql = SAMPLE_SQL[m.samples]
    series = []
    for device, sources in DEVICE_SOURCES.items():
        source = _first_source_with_samples(conn, m.samples, sources, day.isoformat())
        if source is None:
            continue
        rows = conn.execute(sql, (source, day.isoformat())).fetchall()
        points = [(_minute_of_day(r["ts_utc"], day, tz), float(r["v"])) for r in rows if r["v"] is not None]
        points = [(minute, v) for minute, v in points if 0 <= minute < DAY_MIN]
        if points:
            series.append({"device": device, "points": bucket_points(points, cumulative=m.samples in CUMULATIVE)})
    return {"day": day.isoformat(), "metric": key, "series": series, "nights": night_windows(conn, day, tz)}


def night_windows(conn: sqlite3.Connection, day: date, tz: ZoneInfo) -> list[list[float]]:
    """Sleep windows of the watch (or the strap) that touch the local day, as [from, to] minutes clipped to 0–1440."""
    midnight = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    rows = conn.execute(
        "SELECT source, start_utc, end_utc, duration_s FROM sleep WHERE local_date IN (?, ?) ORDER BY start_utc",
        (day.isoformat(), (day + timedelta(days=1)).isoformat()),
    ).fetchall()
    for sources in (GARMIN_PRIORITY, STRAP_PRIORITY):
        chosen = [r for r in rows if r["source"] in sources]
        if chosen:
            break
    else:
        return []
    windows: list[list[float]] = []
    for r in chosen:
        a = (parse_iso_utc(r["start_utc"]).astimezone(tz) - midnight).total_seconds() / 60
        b = (parse_iso_utc(r["end_utc"]).astimezone(tz) - midnight).total_seconds() / 60
        a, b = max(0.0, a), min(float(DAY_MIN), b)
        if b > a:
            windows.append([round(a), round(b)])
    return windows


def days_with_samples(conn: sqlite3.Connection, key: str, start: date, end: date, *, got: dict[str, Any] | None = None) -> list[str]:
    """Days in the range that can show a curve: those with a daily value of the metric (a scan of the sample
    tables over years would take a second; the daily value exists exactly when the watch wrote samples)."""
    m = BY_KEY[key]
    if not m.samples:
        return []
    got = got or metric_series(conn, key, start, end)
    return [d for d, v in zip(got["days"], merged(got["garmin"], got["strap"])) if v is not None]
