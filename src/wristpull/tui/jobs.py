"""Run the long library calls (import, pull, export, report) in worker threads.

The runner owns the job state and the log history; screens render from it. The worker
thread opens its own SQLite connection and reports progress through thread-safe messages.
``JobSpec``, ``JobRecord``, ``describe_result`` and ``last_days`` live in ``wristpull.jobtext``
(Textual-free, shared with the web dashboard) and are re-exported here.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import date
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
from ..importers.strap import StrapStats, pull_strap
from ..coach.runner import CoachResult
from ..jobtext import JobRecord, JobSpec, describe_result, last_days
from ..report import ReportResult
from ..workouts.model import Workout
from ..workouts.push import PushResult, VerifyResult
from ..workouts.watch import RemoveResult, WatchListing
from .deps import TuiDeps
from .i18n import _

__all__ = ["JobLine", "JobRecord", "JobRunner", "JobSpec", "describe_result", "last_days"]

Log = Callable[[str], None]
FINISHED = (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED)


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


# ---- job factories -------------------------------------------------------------------


Stop = Callable[[], bool] | None


def garmin_import_job(config: Config, deps: TuiDeps, *, path: Path | None = None, dry_run: bool = False, stop: Stop = None) -> JobSpec:
    cli = "wristpull import garmin" + (f" --path {path}" if path else "") + (" --dry-run" if dry_run else "")

    def run(conn: sqlite3.Connection, log: Log) -> ImportStats:
        return import_garmin(conn, config, path=path, dry_run=dry_run, log=log, stop=stop, mtp_factory=deps.mtp_factory)

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


def strap_pull_job(config: Config, deps: TuiDeps, *, dry_run: bool = False, since_days: int | None = None, stop: Stop = None) -> JobSpec:
    cli = "wristpull pull strap"
    if since_days is not None:
        cli += f" --since-days {since_days}"
    if dry_run:
        cli += " --dry-run"

    def run(conn: sqlite3.Connection, log: Log) -> StrapStats:
        return pull_strap(conn, config, transport_factory=deps.transport_factory, dry_run=dry_run, since_days=since_days, log=log, stop=stop)

    return JobSpec("pull_strap", _("Pull strap"), cli, run)


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


def workout_push_job(config: Config, deps: TuiDeps, workouts: list[Workout], *, dry_run: bool = False, schedule: bool | None = None, force: bool = False) -> JobSpec:
    from ..workouts.push import push

    files = " ".join(f'"{w.path}"' for w in workouts if w.path)
    cli = f"wristpull workout push {files}" + (" --schedule" if schedule else "") + (" --force" if force else "") + (" --dry-run" if dry_run else "")

    def run(conn: sqlite3.Connection, log: Log) -> PushResult:
        return push(conn, config, workouts, schedule=schedule, dry_run=dry_run, force=force, log=log, mtp_factory=deps.mtp_factory)

    return JobSpec("workout_push", _("Push workouts"), cli, run)


def workout_verify_job(config: Config, deps: TuiDeps) -> JobSpec:
    from ..workouts.push import verify

    def run(conn: sqlite3.Connection, log: Log) -> VerifyResult:
        return verify(conn, config, log=log, mtp_factory=deps.mtp_factory)

    return JobSpec("workout_verify", _("Verify workouts"), "wristpull workout verify", run)


def watch_list_job(config: Config, deps: TuiDeps) -> JobSpec:
    from ..workouts.watch import read_watch

    def run(conn: sqlite3.Connection, log: Log) -> WatchListing:
        log(_("reading GARMIN/Workouts …"))
        return read_watch(conn, config, mtp_factory=deps.mtp_factory)

    return JobSpec("workout_watch", _("Workouts on the watch"), "wristpull workout watch", run)


def watch_remove_job(config: Config, deps: TuiDeps, file_names: list[str]) -> JobSpec:
    from ..workouts.watch import remove_from_watch

    cli = "wristpull workout remove " + " ".join(f'"{n}"' for n in file_names) + " --yes"

    def run(conn: sqlite3.Connection, log: Log) -> RemoveResult:
        return remove_from_watch(conn, config, file_names, log=log, mtp_factory=deps.mtp_factory)

    return JobSpec("workout_remove", _("Remove workouts from the watch"), cli, run)
