"""Adapters from library calls to the plain data the screens render. No SQL in here."""

from __future__ import annotations

import sqlite3
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..aggregate import daily_frame
from ..archive import RawArchive
from ..config import Config
from ..fit.decode import decode_fit, summarize
from ..merge import merged_activities
from ..mtp import probe_mtp
from ..status import collect_status
from .deps import TuiDeps
from .format import fmt_delta, fmt_duration, fmt_float, fmt_int, sparkline
from .i18n import _

KPI_KEYS = ("steps", "resting_hr", "sleep", "body_battery", "stress", "intensity")


@dataclass
class Kpi:
    key: str
    label: str
    value: str
    spark: str
    delta: str


@dataclass
class Overview:
    status: dict[str, Any]
    start: date
    end: date
    kpis: list[Kpi] = field(default_factory=list)
    recent: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _col(rows: list[dict[str, Any]], garmin: str, strap: str | None = None) -> list[float | None]:
    out: list[float | None] = []
    for r in rows:
        value = r.get(garmin)
        if value is None and strap:
            value = r.get(strap)
        out.append(value)
    return out


def _mean(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def _sum(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def day_rows(conn: sqlite3.Connection, start: date, end: date) -> list[dict[str, Any]]:
    """daily_frame as plain dicts with ISO date strings, one per day that has data."""
    frame = daily_frame(conn, start.isoformat(), end.isoformat())
    rows = frame.to_dicts()
    for row in rows:
        row["date"] = row["date"].isoformat()
    return rows


def _intensity(rows: list[dict[str, Any]]) -> list[float | None]:
    out: list[float | None] = []
    for r in rows:
        mod, vig = r.get("garmin_moderate_min"), r.get("garmin_vigorous_min")
        out.append(None if mod is None and vig is None else (mod or 0) + (vig or 0))
    return out


def build_kpis(rows: list[dict[str, Any]], previous: list[dict[str, Any]]) -> list[Kpi]:
    steps, steps_prev = _col(rows, "garmin_steps", "strap_steps"), _col(previous, "garmin_steps", "strap_steps")
    rhr, rhr_prev = _col(rows, "garmin_resting_hr", "strap_resting_hr"), _col(previous, "garmin_resting_hr", "strap_resting_hr")
    sleep, sleep_prev = _col(rows, "garmin_sleep_duration_s", "strap_sleep_duration_s"), _col(previous, "garmin_sleep_duration_s", "strap_sleep_duration_s")
    bb_max, bb_min = _col(rows, "garmin_body_battery_max"), _col(rows, "garmin_body_battery_min")
    stress, stress_prev = _col(rows, "garmin_stress_avg", "strap_stress_avg"), _col(previous, "garmin_stress_avg", "strap_stress_avg")
    intensity, intensity_prev = _intensity(rows), _intensity(previous)
    sleep_mean, sleep_prev_mean = _mean(sleep), _mean(sleep_prev)
    last_bb = next((v for v in reversed(list(zip(bb_max, bb_min))) if v[0] is not None), None)
    return [
        Kpi("steps", _("Steps"), fmt_int(_mean(steps)) + " " + _("avg/day"), sparkline(steps), fmt_delta(_mean(steps), _mean(steps_prev))),
        Kpi("resting_hr", _("Resting HR"), fmt_int(_mean(rhr)) + " bpm", sparkline(rhr), fmt_delta(_mean(rhr), _mean(rhr_prev))),
        Kpi(
            "sleep",
            _("Sleep"),
            fmt_duration(sleep_mean) + " h",
            sparkline(sleep),
            fmt_delta(sleep_mean / 60 if sleep_mean else None, sleep_prev_mean / 60 if sleep_prev_mean else None, suffix=" min"),
        ),
        Kpi(
            "body_battery",
            _("Body battery"),
            (f"{fmt_int(last_bb[0])} ▸ {fmt_int(last_bb[1])}" if last_bb else "–"),
            sparkline(bb_max),
            "",
        ),
        Kpi("stress", _("Stress"), fmt_float(_mean(stress), 0), sparkline(stress), fmt_delta(_mean(stress), _mean(stress_prev))),
        Kpi("intensity", _("Intensity min"), fmt_int(_sum(intensity)), sparkline(intensity), fmt_delta(_sum(intensity), _sum(intensity_prev))),
    ]


def recent_activities(conn: sqlite3.Connection, today: date, *, limit: int = 6, lookback_days: int = 365) -> list[dict[str, Any]]:
    start = today - timedelta(days=lookback_days)
    rows = merged_activities(conn, start.isoformat(), today.isoformat())
    rows.sort(key=lambda r: r["start_utc"], reverse=True)
    return rows[:limit]


def build_notes(status: dict[str, Any]) -> list[str]:
    notes = ["[!] " + _("Sleep, HRV and SpO2 mappings are not yet verified on a real night")]
    tables = status.get("tables", {})
    if tables.get("sleep", {}).get("count", 0) == 0:
        notes.append("[i] " + _("No sleep data yet (watch not worn overnight or Sleep folder empty)"))
    errors = sum(int(e.get("errors", 0)) for e in status.get("raw_files", []))
    if errors:
        notes.append("[i] " + _("{n} raw files with parse errors").format(n=errors))
    if not status.get("garmin_connected") and status.get("garmin_message"):
        notes.append("[i] " + _("Garmin over MTP: {message}").format(message=status["garmin_message"]))
    return notes


def load_overview(conn: sqlite3.Connection, config: Config, today: date, *, days: int = 7, deps: TuiDeps | None = None) -> Overview:
    start = today - timedelta(days=days - 1)
    rows = day_rows(conn, start, today)
    previous = day_rows(conn, start - timedelta(days=days), start - timedelta(days=1))
    deps = deps or TuiDeps()
    probe = (lambda: probe_mtp(deps.mtp_factory())) if deps.mtp_factory else None
    status = collect_status(conn, config, mtp_probe=probe)
    return Overview(
        status=status,
        start=start,
        end=today,
        kpis=build_kpis(rows, previous),
        recent=recent_activities(conn, today),
        notes=build_notes(status),
    )


def raw_file_info(conn: sqlite3.Connection, config: Config, path: Path) -> dict[str, Any] | None:
    row = RawArchive(config.raw_dir, conn).lookup_path(path)
    return dict(row) if row is not None else None


def inspect_fit(path: Path) -> tuple[dict[str, Any], OrderedDict[str, dict[str, Any]]]:
    fit = decode_fit(path)
    header = {
        "type": fit.file_type,
        "created": fit.time_created_iso,
        "manufacturer": fit.manufacturer,
        "product": fit.product_name or fit.product,
        "serial": fit.serial_number,
        "messages": len(fit.messages),
        "warnings": len(fit.warnings),
    }
    return header, summarize(fit)
