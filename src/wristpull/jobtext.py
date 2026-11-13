"""Job records, their human-readable summaries and the JSON form of job results.

Shared by the terminal UI (``tui/jobs.py``) and the web dashboard (``web/jobview.py``) so both show the
same lines for a finished import, pull or push. Textual-free: the only i18n dependency is the gettext
wrapper in ``tui/i18n.py``; the web passes its per-request translation functions instead.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .coach.runner import CoachResult
from .export import ExportResult
from .importers.garmin import ImportStats
from .importers.phone import PullStats
from .importers.strap import StrapStats
from .report import ReportResult
from .tui.format import fmt_int
from .tui.i18n import _ as _gettext
from .tui.i18n import ngettext as _ngettext
from .workouts.push import PushResult, VerifyResult
from .workouts.watch import WORKOUT_LIMIT, RemoveResult, WatchListing, WatchWorkout

Log = Callable[[str], None]
Gettext = Callable[[str], str]
Ngettext = Callable[[str, str, int], str]


@dataclass
class JobSpec:
    key: str
    title: str
    cli: str
    run: Callable[[sqlite3.Connection, Log], Any] | None


@dataclass
class JobRecord:
    spec: JobSpec
    result: Any = None
    error: BaseException | None = None
    duration_s: float = 0.0
    lines: list[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


def last_days(today: date, days: int) -> tuple[date, date]:
    """Inclusive date range of the last ``days`` days ending today."""
    return today - timedelta(days=days - 1), today


NO_DEVICE = "No Garmin device found. Connect the watch (MTP) or set a path."
LIMIT_WARNING = "The fenix 6 stores 20-25 workouts; above that it drops new files silently. Delete old ones under GARMIN/Workouts first."


def describe_result(record: JobRecord, *, gettext: Gettext | None = None, ngettext: Ngettext | None = None) -> list[str]:
    """Human-readable summary lines for a finished job (``gettext``/``ngettext`` default to the TUI's)."""
    _ = gettext or _gettext
    ngettext = ngettext or _ngettext
    if record.error is not None:
        return [f"✗ {record.spec.title}: {record.error}"]
    result = record.result
    lines: list[str] = []
    if getattr(result, "stopped", False) or record.cancelled:
        lines.append("⏹ " + _("stopped on request; files handled so far are kept"))
    if isinstance(result, ImportStats):
        if result.root is None:
            lines.append("✗ " + _(NO_DEVICE))
            if result.device_message:
                lines.append("  " + result.device_message)
            return lines
        lines.append(
            "✓ "
            + _("{found} FIT files, {new} new, {skipped} already archived, {errors} errors").format(
                found=result.found, new=result.new, skipped=result.skipped, errors=result.errors
            )
        )
        for path, err in result.error_files:
            lines.append(f"  ! {path}: {err}")
    elif isinstance(result, PullStats):
        if not result.db_pulled:
            db_state = _("database not found")
        elif result.db_new:
            db_state = _("new database export imported")
        else:
            db_state = _("database unchanged")
        lines.append(
            "✓ "
            + _("phone {device}: {db}; {found} Garmin FIT files, {new} new, {skipped} already archived, {errors} errors").format(
                device=result.device, db=db_state, found=result.fit_found, new=result.fit_new, skipped=result.fit_skipped, errors=result.errors
            )
        )
        for label in result.db_devices:
            lines.append("  " + _("device: {label}").format(label=label))
        for message in result.messages:
            lines.append(f"  ! {message}")
    elif isinstance(result, StrapStats):
        fetched = ", ".join(f"{k}={v}" for k, v in result.fetched.items() if v) or _("nothing new")
        lines.append(
            "✓ "
            + _("strap {device}: {fetched}; {new} new buffers, {skipped} already archived, {errors} errors").format(
                device=result.device or result.address, fetched=fetched, new=result.files_new, skipped=result.files_skipped, errors=result.errors
            )
        )
        for message in result.messages:
            lines.append(f"  ! {message}")
    elif isinstance(result, ExportResult):
        lines.append("✓ " + ngettext("wrote {n} file to {dir}", "wrote {n} files to {dir}", len(result.files)).format(n=len(result.files), dir=result.directory))
    elif isinstance(result, ReportResult):
        lines.append("✓ " + ngettext("wrote {n} file to {dir}", "wrote {n} files to {dir}", len(result.files)).format(n=len(result.files), dir=result.directory))
    elif isinstance(result, CoachResult):
        if result.status == "dry_run":
            lines.append("✓ " + _("dry run: prompt written to {path}").format(path=result.dry_run_path))
        else:
            tokens = f"{result.input_tokens or '?'} in / {result.output_tokens or '?'} out"
            cost = f", ${result.cost_usd:.4f}" if result.cost_usd else ""
            lines.append("✓ " + _("coach {kind} answered in {seconds:.0f} s ({tokens}{cost})").format(kind=result.kind, seconds=result.duration_s, tokens=tokens, cost=cost))
        for warning in result.warnings:
            lines.append(f"  ! {warning}")
    elif isinstance(result, PushResult):
        if result.root is None and not result.invalid:
            lines.append("✗ " + _(NO_DEVICE))
            if result.device_message:
                lines.append("  " + result.device_message)
            return lines
        if result.written:
            prefix = _("dry run: ") if result.dry_run else ""
            lines.append("✓ " + prefix + ngettext("{n} workout file written to {dir}", "{n} workout files written to {dir}", len(result.written)).format(n=len(result.written), dir=result.target))
            if not result.dry_run:
                lines.append("  " + _("Unplug the watch when you are done; it imports the files and lists them under Training > Workouts."))
        for name, errors in result.invalid:
            lines.append(f"  ! {name}: " + "; ".join(errors))
        for name, error in result.errors:
            lines.append(f"  ! {name}: {error}")
    elif isinstance(result, VerifyResult):
        if result.root is None:
            lines.append("✗ " + _(NO_DEVICE))
            if result.device_message:
                lines.append("  " + result.device_message)
            return lines
        lines.append("✓ " + _("{on_watch} workouts on the watch, {verified} newly confirmed, {missing} pushed but not found").format(on_watch=len(result.on_watch), verified=len(result.verified), missing=len(result.missing)))
        if result.near_limit:
            lines.append("  ! " + _(LIMIT_WARNING))
    elif isinstance(result, WatchListing):
        if result.root is None:
            lines.append("✗ " + _(NO_DEVICE))
            if result.device_message:
                lines.append("  " + result.device_message)
            return lines
        lines.append("✓ " + _("{active} active, {pending} waiting for unplugging (of ~{limit})").format(active=len(result.active), pending=len(result.pending), limit=WORKOUT_LIMIT))
        if result.near_limit:
            lines.append("  ! " + _(LIMIT_WARNING))
    elif isinstance(result, RemoveResult):
        if result.listing is not None and result.listing.root is None:
            lines.append("✗ " + _(NO_DEVICE))
            return lines
        lines.append("✓ " + ngettext("removed {n} workout from the watch (backup kept)", "removed {n} workouts from the watch (backup kept)", len(result.removed)).format(n=len(result.removed)))
        for key, error in result.errors:
            lines.append(f"  ! {key}: {error}")
    elif isinstance(result, str):
        lines.append("✓ " + result)
    else:
        lines.append("✓ " + _("done"))
    rows = getattr(result, "rows", None)
    if rows:
        lines.append("  " + _("inserted: ") + ", ".join(f"{k} {fmt_int(v)}" for k, v in sorted(rows.items())))
    return lines


# ---- JSON form of the results (the agent stores them, the web reads them back) ---------------------

RESULT_TYPES: dict[str, type] = {
    "ImportStats": ImportStats,
    "PullStats": PullStats,
    "StrapStats": StrapStats,
    "PushResult": PushResult,
    "VerifyResult": VerifyResult,
    "WatchListing": WatchListing,
    "WatchWorkout": WatchWorkout,
    "RemoveResult": RemoveResult,
}


def jsonable(value: Any) -> Any:
    """``value`` as JSON-ready data: dataclasses become dicts tagged with ``"type"``, paths strings; bytes and
    objects that are neither are dropped (``None``)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return None
    if is_dataclass(value) and not isinstance(value, type):
        name = type(value).__name__
        data: dict[str, Any] = {"type": name} if name in RESULT_TYPES else {}
        for f in fields(value):
            if f.name == "workout" and name == "WatchWorkout":
                continue  # the parsed Workout is rebuilt from the file, not stored
            if f.name == "watch":
                continue  # the open device handle
            data[f.name] = jsonable(getattr(value, f.name))
        return data
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    return str(value)


def _pairs(value: Any) -> Any:
    """JSON lists of two-element lists back to tuples (``error_files``, ``invalid``, ``errors``)."""
    if isinstance(value, list):
        return [tuple(v) if isinstance(v, list) else v for v in value]
    return value


def result_from_json(data: Any) -> Any:
    """The dataclass behind :func:`jsonable` output (unknown fields are ignored, unknown shapes returned as is)."""
    if isinstance(data, list):
        return [result_from_json(v) for v in data]
    if not isinstance(data, dict):
        return data
    cls = RESULT_TYPES.get(str(data.get("type", "")))
    if cls is None:
        return {k: result_from_json(v) for k, v in data.items()}
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        raw = data[f.name]
        if f.name in ("error_files", "invalid", "errors"):
            kwargs[f.name] = [(a, b) for a, b in _pairs(raw)] if isinstance(raw, list) else raw
        elif f.name in ("root", "target", "schedule", "path") and isinstance(raw, str):
            kwargs[f.name] = raw if (f.name == "root" and raw.startswith("mtp://")) else Path(raw)
        elif f.name in ("written", "backups") and isinstance(raw, list):
            kwargs[f.name] = [Path(v) for v in raw]
        elif f.name == "data":
            kwargs[f.name] = b""
        else:
            kwargs[f.name] = result_from_json(raw)
    return cls(**kwargs)
