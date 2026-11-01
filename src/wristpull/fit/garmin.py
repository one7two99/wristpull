"""Map decoded Garmin FIT messages to wristpull table rows.

Message/field numbers follow the Garmin FIT profile plus the undocumented
fields that Gadgetbridge has mapped (stress_level field 3 = body battery).
Pure functions: no database access, easy to unit-test.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..timeutil import iso_utc, local_date, utc_to_fit
from .decode import FitFile, FitMessage

SEMICIRCLE_TO_DEG = 180.0 / 2**31
SLEEP_LEVELS = {0: "unmeasurable", 1: "awake", 2: "light", 3: "deep", 4: "rem"}
SLEEP_SESSION_GAP = timedelta(hours=8)
EVENT_SLEEP = 74  # event message (21) field 0: the watch's sleep window (start/stop), i.e. bedtime to wake-up
from ..rows import ParsedActivity, ParsedRows, ParsedSleep  # noqa: E402  (re-exported)

__all__ = ["ParsedActivity", "ParsedRows", "ParsedSleep", "parse_fit", "compute_timestamp16"]


def _enum(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, (datetime, str, bytes)):
        return None
    if isinstance(value, (list, tuple)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    num = _num(value)
    return None if num is None else int(round(num))


def _deg(semicircles: Any) -> float | None:
    num = _num(semicircles)
    return None if num is None else num * SEMICIRCLE_TO_DEG


def _first(msg: FitMessage, *keys: int | str) -> Any:
    for key in keys:
        value = msg.get(key)
        if value is not None:
            return value
    return None


def _stamp(dt: datetime, tz: ZoneInfo, *, shift: int = 0) -> dict[str, str]:
    return {"ts_utc": iso_utc(dt), "tz": tz.key, "local_date": local_date(dt, tz, shift_seconds=shift)}


def compute_timestamp16(ts16: int, reference: datetime) -> datetime:
    """Reconstruct a full timestamp from the 16-bit ``timestamp_16`` field (Garmin monitoring)."""
    ref_fit = utc_to_fit(reference)
    diff = (ts16 & 0xFFFF) - (ref_fit & 0xFFFF)
    if diff < -32768:
        diff += 65536
    elif diff > 32768:
        diff -= 65536
    return reference + timedelta(seconds=diff)


# --- Activities --------------------------------------------------------------


def _session_row(msg: FitMessage, tz: ZoneInfo, source: str) -> dict[str, Any] | None:
    start = msg.get(2)
    end = msg.timestamp()
    if not isinstance(start, datetime):
        start = end
    if not isinstance(start, datetime):
        return None
    hr_zones = msg.get(65)
    if isinstance(hr_zones, (list, tuple)):
        hr_zones_json = json.dumps([_num(v) for v in hr_zones])
    else:
        hr_zones_json = None
    return {
        "source": source,
        "start_utc": iso_utc(start),
        "end_utc": iso_utc(end) if isinstance(end, datetime) else None,
        "tz": tz.key,
        "local_date": local_date(start, tz),
        "sport": _enum(msg.get(5)),
        "sub_sport": _enum(msg.get(6)),
        "profile_name": _enum(msg.get(110)),
        "elapsed_s": _num(msg.get(7)),
        "timer_s": _num(msg.get(8)),
        "distance_m": _num(msg.get(9)),
        "calories": _int(msg.get(11)),
        "avg_hr": _int(msg.get(16)),
        "max_hr": _int(msg.get(17)),
        "min_hr": _int(msg.get(64)),
        "avg_speed_mps": _num(_first(msg, 124, 14)),
        "max_speed_mps": _num(_first(msg, 125, 15)),
        "ascent_m": _num(msg.get(22)),
        "descent_m": _num(msg.get(23)),
        "avg_cadence": _num(msg.get(18)),
        "max_cadence": _num(msg.get(19)),
        "avg_power_w": _num(msg.get(20)),
        "max_power_w": _num(msg.get(21)),
        "normalized_power_w": _num(msg.get(34)),
        "aerobic_te": _num(msg.get(24)),
        "anaerobic_te": _num(msg.get(137)),
        "training_load": _num(msg.get(168)),
        "training_stress_score": _num(msg.get(35)),
        "avg_temperature_c": _num(msg.get(57)),
        "avg_respiration": _num(_first(msg, 169, 147)),
        "num_laps": _int(msg.get(26)),
        "hr_zone_times_s": hr_zones_json,
    }


def _record_row(msg: FitMessage) -> dict[str, Any] | None:
    ts = msg.timestamp()
    if ts is None:
        return None
    return {
        "ts_utc": iso_utc(ts),
        "hr": _int(msg.get(3)),
        "distance_m": _num(msg.get(5)),
        "speed_mps": _num(_first(msg, 73, 6)),
        "cadence": _num(msg.get(4)),
        "altitude_m": _num(_first(msg, 78, 2)),
        "lat": _deg(msg.get(0)),
        "lon": _deg(msg.get(1)),
        "power_w": _num(msg.get(7)),
        "temperature_c": _num(msg.get(13)),
        "respiration": _num(_first(msg, 108, 99)),
    }


def _lap_row(msg: FitMessage, index: int) -> dict[str, Any]:
    start = msg.get(2)
    end = msg.timestamp()
    return {
        "lap_index": index,
        "start_utc": iso_utc(start) if isinstance(start, datetime) else None,
        "end_utc": iso_utc(end) if isinstance(end, datetime) else None,
        "elapsed_s": _num(msg.get(7)),
        "timer_s": _num(msg.get(8)),
        "distance_m": _num(msg.get(9)),
        "avg_hr": _int(msg.get(15)),
        "max_hr": _int(msg.get(16)),
        "avg_speed_mps": _num(_first(msg, 110, 13)),
        "calories": _int(msg.get(11)),
        "ascent_m": _num(msg.get(21)),
        "descent_m": _num(msg.get(22)),
    }


def parse_activity(fit: FitFile, tz: ZoneInfo, source: str) -> list[ParsedActivity]:
    sessions: list[ParsedActivity] = []
    for msg in fit.of(18):
        row = _session_row(msg, tz, source)
        if row is not None:
            row["device_serial"] = str(fit.serial_number) if fit.serial_number else None
            sessions.append(ParsedActivity(row=row))
    if not sessions:
        return []

    def owner(ts_iso: str | None) -> ParsedActivity:
        if ts_iso is not None:
            for act in sessions:
                end = act.row["end_utc"] or "9999"
                if act.row["start_utc"] <= ts_iso <= end:
                    return act
        return sessions[0]

    for msg in fit.of(20):
        rec = _record_row(msg)
        if rec is not None:
            owner(rec["ts_utc"]).records.append(rec)
    for msg in fit.of(19):
        act = owner(iso_utc(msg.get(2)) if isinstance(msg.get(2), datetime) else None)
        act.laps.append(_lap_row(msg, len(act.laps)))

    # time_in_zone messages (newer firmware) as a fallback for HR zone times
    for msg in fit.of(216):
        if msg.get(0) not in ("session", 18):
            continue
        zones = msg.get(2)
        if isinstance(zones, (list, tuple)) and sessions:
            idx = _int(msg.get(1)) or 0
            target = sessions[idx] if idx < len(sessions) else sessions[0]
            if target.row.get("hr_zone_times_s") is None:
                target.row["hr_zone_times_s"] = json.dumps([_num(v) for v in zones])
    return sessions


# --- Monitoring (daily wellness) --------------------------------------------


def parse_monitoring(fit: FitFile, tz: ZoneInfo, source: str, out: ParsedRows) -> None:
    last_ts: datetime | None = None
    for msg in fit.messages:
        full_ts = msg.timestamp()
        if msg.num == 55:
            ts = full_ts
            if ts is None:
                ts16 = msg.get_raw(26)
                if isinstance(ts16, int) and last_ts is not None:
                    ts = compute_timestamp16(ts16, last_ts)
                elif last_ts is not None:
                    ts = last_ts
            if ts is None:
                continue
            last_ts = ts
            hr = _int(msg.get(27))
            if hr:
                out.heart_rate.append({"source": source, **_stamp(ts, tz), "bpm": hr})
            steps_raw = msg.get_raw(3)
            activity_type = msg.get(5)
            if steps_raw is not None and activity_type is not None:
                out.steps.append(
                    {
                        "source": source,
                        **_stamp(ts, tz, shift=-1),
                        "activity_type": _enum(activity_type),
                        "steps": int(steps_raw),
                        "distance_m": _num(msg.get(2)),
                        "active_time_s": _num(msg.get(4)),
                        "active_calories": _int(msg.get(19)),
                    }
                )
            moderate, vigorous = _int(msg.get(33)), _int(msg.get(34))
            if moderate or vigorous:
                out.intensity_minutes.append(
                    {"source": source, **_stamp(ts, tz), "moderate": moderate or 0, "vigorous": vigorous or 0}
                )
        elif msg.num == 211 and full_ts is not None:
            out.resting_hr.append(
                {
                    "source": source,
                    **_stamp(full_ts, tz),
                    "resting_hr": _int(msg.get(0)),
                    "current_day_resting_hr": _int(msg.get(1)),
                }
            )
        elif msg.num == 227:
            ts = msg.get(1)
            if not isinstance(ts, datetime):
                continue
            level = _int(msg.get(0))
            if level is not None:
                out.stress.append({"source": source, **_stamp(ts, tz), "level": level})
            energy = msg.get_raw(3)
            if isinstance(energy, int) and 0 <= energy <= 100:
                out.body_battery.append({"source": source, **_stamp(ts, tz), "level": energy})
        elif msg.num == 269 and full_ts is not None:
            spo2 = _int(msg.get(0))
            if spo2 and spo2 > 0:
                out.spo2.append(
                    {
                        "source": source,
                        **_stamp(full_ts, tz),
                        "spo2_pct": spo2,
                        "confidence": _int(msg.get(1)),
                        "mode": _enum(msg.get(2)),
                    }
                )
        elif msg.num == 297 and full_ts is not None:
            rate = _num(msg.get(0))
            if rate is not None and rate > 0:
                out.respiration.append({"source": source, **_stamp(full_ts, tz), "breaths_per_min": rate})
        elif msg.num == 370 and full_ts is not None:
            out.hrv.append(
                {
                    "source": source,
                    **_stamp(full_ts, tz),
                    "weekly_avg_ms": _num(msg.get(0)),
                    "last_night_avg_ms": _num(msg.get(1)),
                    "last_night_5min_high_ms": _num(msg.get(2)),
                    "baseline_low_upper_ms": _num(msg.get(3)),
                    "baseline_balanced_lower_ms": _num(msg.get(4)),
                    "baseline_balanced_upper_ms": _num(msg.get(5)),
                    "status": _enum(msg.get(6)),
                }
            )
        elif msg.num == 371 and full_ts is not None:
            value = _num(msg.get(0))
            if value is not None:
                out.hrv_values.append({"source": source, **_stamp(full_ts, tz), "rmssd_ms": value})
        elif msg.num == 229:
            ts = msg.get(0)
            vo2 = _num(msg.get(2))
            if isinstance(ts, datetime) and vo2 is not None:
                out.metrics.append(
                    {"source": source, **_stamp(ts, tz), "metric": "vo2_max", "value": vo2, "sport": _enum(msg.get(5))}
                )


# --- Sleep -------------------------------------------------------------------


def _split_sessions(levels: list[tuple[datetime, str]]) -> list[list[tuple[datetime, str]]]:
    sessions: list[list[tuple[datetime, str]]] = []
    for item in sorted(levels, key=lambda x: x[0]):
        if sessions and item[0] - sessions[-1][-1][0] <= SLEEP_SESSION_GAP:
            sessions[-1].append(item)
        else:
            sessions.append([item])
    return sessions


def sleep_windows(fit: FitFile) -> list[tuple[datetime, datetime]]:
    """(start, stop) pairs of the watch's sleep events; an unmatched start is ignored."""
    windows: list[tuple[datetime, datetime]] = []
    open_start: datetime | None = None
    for msg in sorted(fit.of(21), key=lambda m: m.timestamp() or datetime.min.replace(tzinfo=UTC)):
        ts = msg.timestamp()
        if ts is None or msg.get_raw(0) != EVENT_SLEEP:
            continue
        kind = msg.get(1)
        kind = kind if isinstance(kind, str) else {0: "start", 1: "stop", 4: "stop_all"}.get(kind, str(kind))
        if kind == "start":
            open_start = ts
        elif kind in ("stop", "stop_all", "stop_disable", "stop_disable_all") and open_start is not None:
            windows.append((open_start, ts))
            open_start = None
    return windows


def _window_for(start: datetime, end: datetime, windows: list[tuple[datetime, datetime]]) -> tuple[datetime, datetime] | None:
    for w_start, w_stop in windows:
        if w_start <= end and start <= w_stop:
            return w_start, w_stop
    return None


def parse_sleep(fit: FitFile, tz: ZoneInfo, source: str) -> list[ParsedSleep]:
    levels: list[tuple[datetime, str]] = []
    for msg in fit.of(275):
        ts = msg.timestamp()
        raw = msg.get_raw(0)
        if ts is None or raw is None:
            continue
        levels.append((ts, SLEEP_LEVELS.get(int(raw), str(msg.get(0)))))
    if not levels:
        return []

    assessment = next(iter(fit.of(346)), None)
    summary = next(iter(fit.of(411)), None)
    windows = sleep_windows(fit)
    results: list[ParsedSleep] = []
    for chunk in _split_sessions(levels):
        if not any(stage in ("light", "deep", "rem") for _, stage in chunk):
            continue  # all awake/unmeasurable: no real sleep
        stages: list[dict[str, Any]] = []
        durations: dict[str, int] = defaultdict(int)
        for (ts, stage), nxt in zip(chunk, chunk[1:] + [None]):
            end = nxt[0] if nxt else ts
            seconds = int((end - ts).total_seconds())
            durations[stage] += seconds
            stages.append({"start_utc": iso_utc(ts), "end_utc": iso_utc(end), "stage": stage})
        start, end = chunk[0][0], chunk[-1][0]
        if summary is not None and isinstance(summary.get(10), datetime) and isinstance(summary.get(8), datetime):
            start, end = summary.get(10), summary.get(8)
        window = _window_for(start, end, windows)
        if window is not None:
            # the watch's sleep window (bedtime to wake-up) is what Garmin shows as the sleep start/end;
            # minutes it does not cover with a stage record are kept visible as "unmeasurable"
            w_start, w_stop = window
            if w_start < start:
                stages.insert(0, {"start_utc": iso_utc(w_start), "end_utc": iso_utc(start), "stage": "unmeasurable"})
                durations["unmeasurable"] += int((start - w_start).total_seconds())
            if w_stop > end:
                stages.append({"start_utc": iso_utc(end), "end_utc": iso_utc(w_stop), "stage": "unmeasurable"})
                durations["unmeasurable"] += int((w_stop - end).total_seconds())
            start, end = min(start, w_start), max(end, w_stop)
        row = {
            "source": source,
            "start_utc": iso_utc(start),
            "end_utc": iso_utc(end),
            "tz": tz.key,
            "local_date": local_date(end, tz),
            "duration_s": int((end - start).total_seconds()) - durations.get("awake", 0),
            "deep_s": durations.get("deep", 0),
            "light_s": durations.get("light", 0),
            "rem_s": durations.get("rem", 0),
            "awake_s": durations.get("awake", 0),
            "unmeasurable_s": durations.get("unmeasurable", 0),
            "score": None,
            "quality_score": None,
            "recovery_score": None,
            "awakenings": None,
            "avg_stress": None,
        }
        if assessment is not None:
            row.update(
                {
                    "score": _int(assessment.get(6)),
                    "quality_score": _int(assessment.get(7)),
                    "recovery_score": _int(assessment.get(8)),
                    "awakenings": _int(assessment.get(11)),
                    "avg_stress": _num(assessment.get(15)),
                }
            )
        if summary is not None:
            for key, num in (("score", 0), ("deep_s", 1), ("light_s", 2), ("rem_s", 3), ("awake_s", 4), ("unmeasurable_s", 5)):
                value = _int(summary.get(num))
                if value is not None:
                    row[key] = value
        results.append(ParsedSleep(row=row, stages=stages))
    return results


# --- Entry point -------------------------------------------------------------


def parse_fit(fit: FitFile, tz: ZoneInfo, source: str) -> ParsedRows:
    """Extract everything wristpull knows about from one decoded FIT file."""
    out = ParsedRows()
    out.activities = parse_activity(fit, tz, source)
    out.sleep = parse_sleep(fit, tz, source)
    parse_monitoring(fit, tz, source, out)
    return out
