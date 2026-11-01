"""Rebuild the derived daily_summary table from the sample tables."""

from __future__ import annotations

import sqlite3

from .timeutil import iso_utc, now_utc


_DAY_TABLES = ("steps", "heart_rate", "stress", "body_battery", "sleep", "activities", "spo2", "respiration", "hrv", "resting_hr", "intensity_minutes", "temperature")


def _days(conn: sqlite3.Connection, source: str) -> list[tuple[str, str]]:
    sql = " UNION ".join(
        f"SELECT local_date, tz FROM {t} WHERE source = ?"
        for t in _DAY_TABLES
    )
    rows = conn.execute(f"SELECT DISTINCT local_date, tz FROM ({sql}) ORDER BY local_date", [source] * len(_DAY_TABLES)).fetchall()
    seen: dict[str, str] = {}
    for date, tz in rows:
        seen.setdefault(date, tz)
    return sorted(seen.items())


def summarize_day(conn: sqlite3.Connection, source: str, day: str) -> dict[str, object]:
    p = (source, day)
    steps = conn.execute(
        """SELECT SUM(steps), SUM(distance_m), SUM(active_calories), SUM(active_time_s) FROM (
               SELECT MAX(steps) AS steps, MAX(distance_m) AS distance_m,
                      MAX(active_calories) AS active_calories, MAX(active_time_s) AS active_time_s
               FROM steps WHERE source = ? AND local_date = ? GROUP BY activity_type)""",
        p,
    ).fetchone()
    intensity = conn.execute(
        "SELECT SUM(moderate), SUM(vigorous) FROM intensity_minutes WHERE source = ? AND local_date = ?", p
    ).fetchone()
    hr = conn.execute(
        "SELECT MIN(bpm), MAX(bpm), AVG(bpm) FROM heart_rate WHERE source = ? AND local_date = ?", p
    ).fetchone()
    resting = conn.execute(
        """SELECT COALESCE(current_day_resting_hr, resting_hr) FROM resting_hr
           WHERE source = ? AND local_date = ? ORDER BY ts_utc DESC LIMIT 1""",
        p,
    ).fetchone()
    stress = conn.execute(
        "SELECT AVG(level), MAX(level) FROM stress WHERE source = ? AND local_date = ? AND level >= 0", p
    ).fetchone()
    bb = conn.execute(
        "SELECT MIN(level), MAX(level) FROM body_battery WHERE source = ? AND local_date = ?", p
    ).fetchone()
    spo2 = conn.execute(
        "SELECT AVG(spo2_pct), MIN(spo2_pct) FROM spo2 WHERE source = ? AND local_date = ?", p
    ).fetchone()
    resp = conn.execute(
        "SELECT AVG(breaths_per_min) FROM respiration WHERE source = ? AND local_date = ?", p
    ).fetchone()
    temp = conn.execute("SELECT AVG(celsius) FROM temperature WHERE source = ? AND local_date = ?", p).fetchone()
    sleep = conn.execute(
        "SELECT SUM(duration_s), MAX(score) FROM sleep WHERE source = ? AND local_date = ?", p
    ).fetchone()
    hrv = conn.execute(
        "SELECT last_night_avg_ms FROM hrv WHERE source = ? AND local_date = ? ORDER BY ts_utc DESC LIMIT 1", p
    ).fetchone()
    if not hrv or hrv[0] is None:
        # no nightly summary (e.g. the strap): average the 5-minute RMSSD samples inside that day's sleep
        hrv = conn.execute(
            """SELECT AVG(v.rmssd_ms) FROM hrv_values v JOIN sleep s ON s.source = v.source
               AND v.ts_utc BETWEEN s.start_utc AND s.end_utc
               WHERE s.source = ? AND s.local_date = ?""",
            p,
        ).fetchone()
    acts = conn.execute(
        """SELECT COUNT(*), SUM(COALESCE(timer_s, elapsed_s)), SUM(distance_m), SUM(training_load)
           FROM activities WHERE source = ? AND local_date = ?""",
        p,
    ).fetchone()
    return {
        "steps": steps[0],
        "distance_m": steps[1],
        "active_calories": steps[2],
        "active_time_s": steps[3],
        "moderate_min": intensity[0],
        "vigorous_min": intensity[1],
        "hr_min": hr[0],
        "hr_max": hr[1],
        "hr_avg": round(hr[2], 1) if hr[2] is not None else None,
        "resting_hr": resting[0] if resting else None,
        "stress_avg": round(stress[0], 1) if stress[0] is not None else None,
        "stress_max": stress[1],
        "body_battery_min": bb[0],
        "body_battery_max": bb[1],
        "spo2_avg": round(spo2[0], 1) if spo2[0] is not None else None,
        "spo2_min": spo2[1],
        "respiration_avg": round(resp[0], 1) if resp[0] is not None else None,
        "temperature_avg": round(temp[0], 2) if temp[0] is not None else None,
        "sleep_duration_s": sleep[0],
        "sleep_score": sleep[1],
        "hrv_last_night_avg_ms": round(hrv[0], 1) if hrv and hrv[0] is not None else None,
        "activities_count": acts[0] or 0,
        "activities_duration_s": acts[1],
        "activities_distance_m": acts[2],
        "training_load": acts[3],
    }


def rebuild_daily_summary(conn: sqlite3.Connection, *, source: str, days: list[str] | None = None) -> int:
    """Recompute daily_summary rows for the given days (default: every day with data)."""
    all_days = _days(conn, source)
    if days is not None:
        wanted = set(days)
        all_days = [(d, tz) for d, tz in all_days if d in wanted]
    stamp = iso_utc(now_utc())
    for day, tz in all_days:
        values = summarize_day(conn, source, day)
        cols = ["source", "local_date", "tz", *values.keys(), "updated_at_utc"]
        params = [source, day, tz, *values.values(), stamp]
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in ("source", "local_date"))
        conn.execute(
            f"INSERT INTO daily_summary ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
            f"ON CONFLICT(source, local_date) DO UPDATE SET {updates}",
            params,
        )
    return len(all_days)
