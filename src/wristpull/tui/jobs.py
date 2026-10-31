"""Run the long library calls (import, pull, export, report) in worker threads.

The runner owns the job state and the log history; screens render from it. The worker
thread opens its own SQLite connection and reports progress through thread-safe messages.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from textual.message import Message
from textual.worker import Worker, WorkerState

from ..config import Config
from ..db import connect
from ..export import ExportResult
from ..importers.garmin import ImportStats, import_garmin
from ..importers.phone import PullStats, pull_phone
from ..report import ReportResult
from .deps import TuiDeps
from .format import fmt_int
from .i18n import _, ngettext

Log = Callable[[str], None]
FINISHED = (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED)


@dataclass
class JobSpec:
    key: str
    title: str
    cli: str
    run: Callable[[sqlite3.Connection, Log], Any]


@dataclass
class JobRecord:
    spec: JobSpec
    result: Any = None
    error: BaseException | None = None
    duration_s: float = 0.0
    lines: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None


class JobLine(Message):
    """One log line from the running job (posted from the worker thread)."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class JobRunner:
    """One job at a time; keeps the log and the last result for the screens."""

    def __init__(self, app, db_path: Path) -> None:
        self.app = app
        self.db_path = Path(db_path)
        self.active: JobSpec | None = None
        self.started_at: float = 0.0
        self.lines: list[str] = []
        self.last: JobRecord | None = None

    @property
    def busy(self) -> bool:
        return self.active is not None

    def start(self, spec: JobSpec) -> bool:
        if self.active is not None:
            return False
        self.active = spec
        self.started_at = time.monotonic()
        self.lines.append(f"▶ {spec.title}   $ {spec.cli}")
        self.app.run_worker(partial(self._work, spec), name=spec.key, group="job", thread=True, exit_on_error=False)
        return True

    def _work(self, spec: JobSpec) -> Any:
        conn = connect(self.db_path)
        try:
            return spec.run(conn, self._log)
        finally:
            conn.close()

    def _log(self, text: str) -> None:
        self.app.post_message(JobLine(text))  # thread-safe

    def on_line(self, text: str) -> None:
        self.lines.append(text)

    def finish(self, worker: Worker) -> JobRecord | None:
        """Called on the UI thread when the job worker reaches a final state."""
        if self.active is None or worker.state not in FINISHED:
            return None
        spec, self.active = self.active, None
        record = JobRecord(spec, duration_s=time.monotonic() - self.started_at)
        if worker.state == WorkerState.SUCCESS:
            record.result = worker.result
        else:
            record.error = worker.error or RuntimeError(_("cancelled"))
        record.lines = describe_result(record)
        self.lines.extend(record.lines)
        self.last = record
        return record


def describe_result(record: JobRecord) -> list[str]:
    """Human-readable summary lines for a finished job."""
    if record.error is not None:
        return [f"✗ {record.spec.title}: {record.error}"]
    result = record.result
    lines: list[str] = []
    if isinstance(result, ImportStats):
        if result.root is None:
            lines.append("✗ " + _("No Garmin device found. Connect the watch (MTP) or set a path."))
            if result.mount_message:
                lines.append("  " + result.mount_message)
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
    elif isinstance(result, ExportResult):
        lines.append("✓ " + ngettext("wrote {n} file to {dir}", "wrote {n} files to {dir}", len(result.files)).format(n=len(result.files), dir=result.directory))
    elif isinstance(result, ReportResult):
        lines.append("✓ " + ngettext("wrote {n} file to {dir}", "wrote {n} files to {dir}", len(result.files)).format(n=len(result.files), dir=result.directory))
    else:
        lines.append("✓ " + _("done"))
    rows = getattr(result, "rows", None)
    if rows:
        lines.append("  " + _("inserted: ") + ", ".join(f"{k} {fmt_int(v)}" for k, v in sorted(rows.items())))
    return lines


# ---- job factories -------------------------------------------------------------------


def garmin_import_job(config: Config, deps: TuiDeps, *, path: Path | None = None, dry_run: bool = False) -> JobSpec:
    cli = "wristpull import garmin" + (f" --path {path}" if path else "") + (" --dry-run" if dry_run else "")

    def run(conn: sqlite3.Connection, log: Log) -> ImportStats:
        return import_garmin(conn, config, path=path, dry_run=dry_run, log=log, roots=deps.roots, mounter=deps.mounter)

    return JobSpec("import_garmin", _("Import Garmin"), cli, run)


def phone_pull_job(
    config: Config,
    deps: TuiDeps,
    *,
    serial: str | None = None,
    trigger: bool = True,
    sync: bool = True,
    wait: float = 60.0,
    dry_run: bool = False,
) -> JobSpec:
    cfg = replace(config, phone_serial=serial) if serial else config
    cli = "wristpull pull phone"
    if serial:
        cli += f" --serial {serial}"
    if not trigger:
        cli += " --no-trigger"
    elif not sync:
        cli += " --no-sync"
    if wait != 60.0:
        cli += f" --wait {wait:g}"
    if dry_run:
        cli += " --dry-run"

    def run(conn: sqlite3.Connection, log: Log) -> PullStats:
        return pull_phone(conn, cfg, adb=deps.adb_factory(), trigger=trigger, sync=sync, wait=wait, dry_run=dry_run, log=log, sleep=deps.sleep)

    return JobSpec("pull_phone", _("Pull phone"), cli, run)


def export_job(config: Config, start: date, end: date) -> JobSpec:
    from ..export import export_for_claude

    cli = f"wristpull export --for-claude --from {start.isoformat()} --to {end.isoformat()}"

    def run(conn: sqlite3.Connection, log: Log) -> ExportResult:
        log(_("exporting {start} to {end} …").format(start=start.isoformat(), end=end.isoformat()))
        return export_for_claude(conn, config, start.isoformat(), end.isoformat())

    return JobSpec("export", _("Export for Claude"), cli, run)


def report_job(config: Config, kind: str, day: date, *, charts: bool = True) -> JobSpec:
    from ..report import write_report

    cli = f"wristpull report {kind} --date {day.isoformat()}" + (" --charts" if charts else "")

    def run(conn: sqlite3.Connection, log: Log) -> ReportResult:
        log(_("rendering {kind} report for {day} …").format(kind=kind, day=day.isoformat()))
        return write_report(conn, config, kind, day, charts=charts)

    title = _("Weekly report") if kind == "week" else _("Monthly report")
    return JobSpec(f"report_{kind}", title, cli, run)


def last_days(today: date, days: int) -> tuple[date, date]:
    """Inclusive date range of the last ``days`` days ending today."""
    return today - timedelta(days=days - 1), today
