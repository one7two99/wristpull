"""Read-only detail queries for one activity, one night or one day (used by the TUI and exports)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

from .timeutil import parse_iso_utc


def _rows(conn: sqlite3.Connection, sql: str, params: tuple) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def activity_detail(conn: sqlite3.Connection, activity_id: int) -> dict[str, Any] | None:
    """The activity row plus laps, HR zone seconds and a per-record series (ts, hr, speed, altitude)."""
    row = conn.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
    if row is None:
        return None
    detail = dict(row)
    detail["laps"] = _rows(conn, "SELECT * FROM activity_laps WHERE activity_id = ? ORDER BY lap_index", (activity_id,))
    detail["records"] = _rows(
        conn,
        "SELECT ts_utc, hr, speed_mps, altitude_m, cadence, power_w FROM activity_records WHERE activity_id = ? ORDER BY ts_utc",
        (activity_id,),
    )
    zones: list[float] = []
    if detail.get("hr_zone_times_s"):
        try:
            zones = [float(z or 0) for z in json.loads(detail["hr_zone_times_s"])]
        except ValueError:
            zones = []
    detail["hr_zones_s"] = zones
    raw = conn.execute("SELECT archive_path FROM raw_files WHERE id = ?", (detail.get("raw_file_id"),)).fetchone()
    detail["archive_path"] = raw["archive_path"] if raw else None
    return detail


def downsample(values: list[float | None], buckets: int) -> list[float | None]:
    """Average ``values`` into at most ``buckets`` groups (None-aware); short inputs pass through."""
    if buckets <= 0 or len(values) <= buckets:
        return list(values)
    out: list[float | None] = []
    size = len(values) / buckets
    for i in range(buckets):
        chunk = [v for v in values[int(i * size) : int((i + 1) * size)] if v is not None]
        out.append(sum(chunk) / len(chunk) if chunk else None)
    return out


def sleep_night(conn: sqlite3.Connection, sleep_id: int) -> dict[str, Any] | None:
    """The sleep row plus its stages and the HRV/SpO2/respiration samples of the same source inside the window."""
    row = conn.execute("SELECT * FROM sleep WHERE id = ?", (sleep_id,)).fetchone()
    if row is None:
        return None
    night = dict(row)
    night["stages"] = _rows(conn, "SELECT start_utc, end_utc, stage FROM sleep_stages WHERE sleep_id = ? ORDER BY start_utc", (sleep_id,))
    window = (night["source"], night["start_utc"], night["end_utc"])
    hrv = conn.execute(
        "SELECT COUNT(*), AVG(rmssd_ms), MIN(rmssd_ms), MAX(rmssd_ms) FROM hrv_values WHERE source = ? AND ts_utc BETWEEN ? AND ?", window
    ).fetchone()
    spo2 = conn.execute(
        "SELECT COUNT(*), AVG(spo2_pct), MIN(spo2_pct) FROM spo2 WHERE source = ? AND ts_utc BETWEEN ? AND ?", window
    ).fetchone()
    resp = conn.execute(
        "SELECT COUNT(*), AVG(breaths_per_min) FROM respiration WHERE source = ? AND ts_utc BETWEEN ? AND ?", window
    ).fetchone()
    hr = conn.execute("SELECT COUNT(*), MIN(bpm), AVG(bpm) FROM heart_rate WHERE source = ? AND ts_utc BETWEEN ? AND ?", window).fetchone()
    summary = conn.execute(
        "SELECT last_night_avg_ms, last_night_5min_high_ms, status FROM hrv WHERE source = ? AND local_date = ? ORDER BY ts_utc DESC LIMIT 1",
        (night["source"], night["local_date"]),
    ).fetchone()
    night["hrv"] = {"n": hrv[0], "avg": hrv[1], "min": hrv[2], "max": hrv[3], "summary": dict(summary) if summary else None}
    night["spo2"] = {"n": spo2[0], "avg": spo2[1], "min": spo2[2]}
    night["respiration"] = {"n": resp[0], "avg": resp[1]}
    night["heart_rate"] = {"n": hr[0], "min": hr[1], "avg": hr[2]}
    return night


def nights(conn: sqlite3.Connection, start: str, end: str) -> list[dict[str, Any]]:
    """All sleep rows (every source) whose wake-up date lies in the range, newest first."""
    return _rows(conn, "SELECT * FROM sleep WHERE local_date BETWEEN ? AND ? ORDER BY local_date DESC, source", (start, end))


def activities(conn: sqlite3.Connection, start: str, end: str, *, sport: str | None = None) -> list[dict[str, Any]]:
    """Activities of every source in the range, newest first, optionally filtered by sport substring."""
    rows = _rows(conn, "SELECT * FROM activities WHERE local_date BETWEEN ? AND ? ORDER BY start_utc DESC", (start, end))
    if sport:
        needle = sport.lower()
        rows = [r for r in rows if needle in ((r.get("sport") or "") + " " + (r.get("profile_name") or "")).lower()]
    return rows


def sports(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT sport FROM activities WHERE sport IS NOT NULL ORDER BY sport").fetchall()]


def local_clock(ts_utc: str, tz: ZoneInfo) -> str:
    return parse_iso_utc(ts_utc).astimezone(tz).strftime("%H:%M")


# ---- one day, watch and strap side by side (the TUI Days screen and the web Days page) ----------

DAY_DETAIL_FIELDS: tuple[tuple[str, str, str], ...] = (
    # (label msgid, daily_summary column, kind) kind: int | float | duration | km | hr | stress | bb | spo2
    ("Steps", "steps", "int"),
    ("Distance", "distance_m", "km"),
    ("Active calories", "active_calories", "int"),
    ("Moderate minutes", "moderate_min", "int"),
    ("Vigorous minutes", "vigorous_min", "int"),
    ("HR min / avg / max", "hr", "hr"),
    ("Resting HR", "resting_hr", "int"),
    ("Stress avg / max", "stress", "stress"),
    ("Body battery min / max", "body_battery", "bb"),
    ("SpO2 avg / min", "spo2", "spo2"),
    ("Respiration avg", "respiration_avg", "float"),
    ("Temperature avg", "temperature_avg", "float"),
    ("Sleep", "sleep_duration_s", "duration"),
    ("Sleep score", "sleep_score", "int"),
    ("HRV last night", "hrv_last_night_avg_ms", "float"),
    ("Activities", "activities_count", "int"),
    ("Training load", "training_load", "float"),
)


def day_detail_value(row: dict[str, Any], prefix: str, column: str, kind: str, lang: str | None = None) -> str:
    """One formatted cell of the day detail (``prefix`` garmin or strap); ``–`` when absent."""
    from .tui.format import fmt_duration, fmt_float, fmt_int  # plain formatting helpers, no Textual

    g = lambda name: row.get(f"{prefix}_{name}")  # noqa: E731
    if kind == "int":
        return fmt_int(g(column), lang)
    if kind == "float":
        return fmt_float(g(column), 1, lang)
    if kind == "km":
        v = g(column)
        return "–" if v is None else f"{fmt_float(v / 1000, 1, lang)} km"
    if kind == "duration":
        v = g(column)
        return "–" if v is None else fmt_duration(v) + " h"
    if kind == "hr":
        return f"{fmt_int(g('hr_min'), lang)} / {fmt_int(g('hr_avg'), lang)} / {fmt_int(g('hr_max'), lang)}"
    if kind == "stress":
        return f"{fmt_float(g('stress_avg'), 0, lang)} / {fmt_int(g('stress_max'), lang)}"
    if kind == "bb":
        return f"{fmt_int(g('body_battery_min'), lang)} / {fmt_int(g('body_battery_max'), lang)}"
    if kind == "spo2":
        return f"{fmt_float(g('spo2_avg'), 0, lang)} / {fmt_int(g('spo2_min'), lang)}"
    return "–"


def day_detail_rows(row: dict[str, Any], lang: str | None = None) -> list[dict[str, str]]:
    """The fields of one ``daily_frame`` row that have a value, as ``{label, garmin, strap}`` (label = msgid)."""
    out = []
    for label, column, kind in DAY_DETAIL_FIELDS:
        garmin = day_detail_value(row, "garmin", column, kind, lang)
        strap = day_detail_value(row, "strap", column, kind, lang)
        if garmin == "–" and strap == "–":
            continue
        out.append({"label": label, "garmin": garmin, "strap": strap})
    return out
