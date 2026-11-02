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
from ..coach.runner import CoachResult
from ..report import ReportResult
from ..workouts.model import Workout
from ..workouts.push import PushResult, VerifyResult
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
    cancelled: bool = False

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
        self.cancel_requested = False

    @property
    def busy(self) -> bool:
        return self.active is not None

    def should_stop(self) -> bool:
        """Polled by the running job between files (cooperative cancel)."""
        return self.cancel_requested

    def cancel(self) -> bool:
        """Ask the running job to stop at the next opportunity; returns False when nothing runs."""
        if self.active is None:
            return False
        self.cancel_requested = True
        self.lines.append("⏹ " + _("stop requested, finishing the current file …"))
        return True

    def start(self, spec: JobSpec) -> bool:
        if self.active is not None:
            return False
        self.active = spec
        self.cancel_requested = False
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
        record = JobRecord(spec, duration_s=time.monotonic() - self.started_at, cancelled=self.cancel_requested)
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
    if getattr(result, "stopped", False) or record.cancelled:
        lines.append("⏹ " + _("stopped on request; files handled so far are kept"))
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
            lines.append("✗ " + _("No Garmin device found. Connect the watch (MTP) or set a path."))
            if result.mount_message:
                lines.append("  " + result.mount_message)
            return lines
        if result.written:
            prefix = _("dry run: ") if result.dry_run else ""
            lines.append("✓ " + prefix + ngettext("{n} workout file written to {dir}", "{n} workout files written to {dir}", len(result.written)).format(n=len(result.written), dir=result.target))
            if not result.dry_run:
                lines.append("  " + _("Unplug the watch; it imports the files and lists them under Training > Workouts."))
        for name, errors in result.invalid:
            lines.append(f"  ! {name}: " + "; ".join(errors))
    elif isinstance(result, VerifyResult):
        if result.root is None:
            lines.append("✗ " + _("No Garmin device found. Connect the watch (MTP) or set a path."))
            if result.mount_message:
                lines.append("  " + result.mount_message)
            return lines
        lines.append("✓ " + _("{on_watch} workouts on the watch, {verified} newly confirmed, {missing} pushed but not found").format(on_watch=len(result.on_watch), verified=len(result.verified), missing=len(result.missing)))
    elif isinstance(result, str):
        lines.append("✓ " + result)
    else:
        lines.append("✓ " + _("done"))
    rows = getattr(result, "rows", None)
    if rows:
        lines.append("  " + _("inserted: ") + ", ".join(f"{k} {fmt_int(v)}" for k, v in sorted(rows.items())))
    return lines


# ---- job factories -------------------------------------------------------------------


Stop = Callable[[], bool] | None


def garmin_import_job(config: Config, deps: TuiDeps, *, path: Path | None = None, dry_run: bool = False, stop: Stop = None) -> JobSpec:
    cli = "wristpull import garmin" + (f" --path {path}" if path else "") + (" --dry-run" if dry_run else "")

    def run(conn: sqlite3.Connection, log: Log) -> ImportStats:
        return import_garmin(conn, config, path=path, dry_run=dry_run, log=log, roots=deps.roots, mounter=deps.mounter, stop=stop)

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
    stop: Stop = None,
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
        return pull_phone(conn, cfg, adb=deps.adb_factory(), trigger=trigger, sync=sync, wait=wait, dry_run=dry_run, log=log, sleep=deps.sleep, stop=stop)

    return JobSpec("pull_phone", _("Pull phone"), cli, run)


def export_job(config: Config, start: date, end: date, *, write_csv: bool = True) -> JobSpec:
    from ..export import export_for_claude

    cli = f"wristpull export --for-claude --from {start.isoformat()} --to {end.isoformat()}" + ("" if write_csv else " --no-csv")

    def run(conn: sqlite3.Connection, log: Log) -> ExportResult:
        log(_("exporting {start} to {end} …").format(start=start.isoformat(), end=end.isoformat()))
        return export_for_claude(conn, config, start.isoformat(), end.isoformat(), write_csv=write_csv)

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


def coach_job(config: Config, deps: TuiDeps, kind: str, *, today: date, question: str | None = None, dry_run: bool = False, language: str | None = None, force: bool = True) -> JobSpec:
    from ..coach.backends import make_backend
    from ..coach.runner import run_coach

    cli = f"wristpull coach {kind}" + (f' "{question}"' if question else "") + (" --dry-run" if dry_run else "") + (f" --lang {language}" if language else "")
    titles = {"daily": _("Coach: daily check"), "weekly": _("Coach: weekly review"), "ask": _("Coach: question")}

    def run(conn: sqlite3.Connection, log: Log) -> CoachResult:
        factory = deps.coach_backend or (lambda c, **kw: make_backend(c, **kw))
        backend = factory(config, kind=kind)
        return run_coach(conn, config, kind, today=today, language=language, question=question, backend=backend, dry_run=dry_run, force=force, log=log)

    return JobSpec(f"coach_{kind}", titles.get(kind, kind), cli, run)


def workout_push_job(config: Config, deps: TuiDeps, workouts: list[Workout], *, dry_run: bool = False, schedule: bool | None = None) -> JobSpec:
    from ..workouts.push import push

    files = " ".join(f'"{w.path}"' for w in workouts if w.path)
    cli = f"wristpull workout push {files}" + (" --schedule" if schedule else "") + (" --dry-run" if dry_run else "")

    def run(conn: sqlite3.Connection, log: Log) -> PushResult:
        return push(conn, config, workouts, roots=deps.roots, mounter=deps.mounter, schedule=schedule, dry_run=dry_run, log=log)

    return JobSpec("workout_push", _("Push workouts"), cli, run)


def workout_verify_job(config: Config, deps: TuiDeps) -> JobSpec:
    from ..workouts.push import verify

    def run(conn: sqlite3.Connection, log: Log) -> VerifyResult:
        return verify(conn, config, roots=deps.roots, mounter=deps.mounter, log=log)

    return JobSpec("workout_verify", _("Verify workouts"), "wristpull workout verify", run)
