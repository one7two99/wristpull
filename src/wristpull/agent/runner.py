"""The agent loop: heartbeat, USB presence, one queued job at a time.

``AgentRunner.run_once()`` is one cycle (tests call it directly); ``serve()`` repeats it every
``poll`` seconds until stopped. Every job reloads the config file, so settings saved in the browser
apply to the next job. The job's log lines and the heartbeat are written through the job's own
connection and commit when the library commits (see ``queue.append_log``).
"""

from __future__ import annotations

import signal
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

from .. import __version__
from ..adb import adb_usb_devices
from ..config import Config, load_config
from ..db import connect
from ..jobtext import jsonable
from . import queue
from .deps import AgentDeps

GARMIN_VENDOR = "091e"
RESTART_MESSAGE = "the agent was restarted while this job ran"
Log = Callable[[str], None]


def usb_vendor_present(sysfs: Path, vendor: str = GARMIN_VENDOR) -> bool:
    """Is a USB device of ``vendor`` attached (sysfs only, no device access)?"""
    if not sysfs.is_dir():
        return False
    for dev in sysfs.iterdir():
        try:
            if (dev / "idVendor").read_text().strip().lower() == vendor:
                return True
        except OSError:
            continue
    return False


def run_kind(conn: sqlite3.Connection, config: Config, deps: AgentDeps, kind: str, params: dict[str, Any], *, log: Log, stop: Callable[[], bool]) -> Any:
    """The library call behind each job kind (the same calls the TUI's job factories make)."""
    if kind == "import_garmin":
        from ..importers.garmin import import_garmin

        return import_garmin(conn, config, dry_run=params["dry_run"], log=log, stop=stop, mtp_factory=deps.mtp_factory)
    if kind == "pull_phone":
        from ..importers.phone import pull_phone

        cfg = replace(config, phone_serial=params["serial"]) if params["serial"] else config
        return pull_phone(conn, cfg, adb=deps.adb_factory(), trigger=params["trigger"], sync=params["sync"], wait=float(params["wait"]), dry_run=params["dry_run"], log=log, sleep=deps.sleep, stop=stop)
    if kind == "pull_strap":
        from ..importers.strap import pull_strap

        return pull_strap(conn, config, transport_factory=deps.transport_factory, dry_run=params["dry_run"], since_days=params["since_days"], log=log, stop=stop)
    if kind in ("workout_push", "workout_push_upcoming"):
        from ..workouts.push import push
        from ..workouts.store import load, upcoming, workout_file

        if kind == "workout_push":
            workouts = [load(workout_file(config, name)) for name in params["names"]]
        else:
            workouts = upcoming(config, conn, deps.today(), force=params["force"])
        return push(conn, config, workouts, dry_run=params["dry_run"], force=params["force"], schedule=params.get("schedule") or None, log=log, mtp_factory=deps.mtp_factory)
    if kind == "workout_verify":
        from ..workouts.push import verify

        return verify(conn, config, log=log, mtp_factory=deps.mtp_factory)
    if kind == "watch_list":
        from ..workouts.watch import read_watch

        log("reading GARMIN/Workouts …")
        return read_watch(conn, config, mtp_factory=deps.mtp_factory)
    if kind == "watch_remove":
        from ..workouts.watch import remove_from_watch

        return remove_from_watch(conn, config, params["keys"], log=log, mtp_factory=deps.mtp_factory)
    raise ValueError(f"unknown job kind: {kind}")


class AgentRunner:
    def __init__(self, config_path: Path | None = None, deps: AgentDeps | None = None, *, load: Callable[[Path | None], Config] = load_config) -> None:
        self.config_path = config_path
        self.deps = deps or AgentDeps()
        self.load = load
        self.config = load(config_path)
        self.stop_serving = False
        self.cycles = 0

    @property
    def db_path(self) -> Path:
        return self.config.db_path

    def start(self) -> list[int]:
        """Called once before the loop: jobs left running by a dead agent become errors."""
        with closing(connect(self.db_path)) as conn:
            stale = queue.fail_stale(conn, RESTART_MESSAGE, now=self.deps.now())
            queue.set_state(conn, {"agent_version": __version__}, now=self.deps.now())
        return stale

    def heartbeat(self, conn: sqlite3.Connection) -> dict[str, str]:
        values = {
            "heartbeat": "1",
            "watch_present": "1" if usb_vendor_present(self.deps.sysfs) else "0",
            "phone_present": "1" if adb_usb_devices(self.deps.sysfs) else "0",
        }
        queue.set_state(conn, values, now=self.deps.now())
        return values

    def run_once(self) -> queue.AgentJob | None:
        """One cycle: heartbeat and USB presence, then the oldest queued job, if any. Returns the finished job."""
        self.cycles += 1
        with closing(connect(self.db_path)) as conn:
            self.heartbeat(conn)
            job = queue.claim(conn, now=self.deps.now())
            if job is None:
                return None
            return self._run_job(conn, job)

    def _run_job(self, conn: sqlite3.Connection, job: queue.AgentJob) -> queue.AgentJob:
        self.config = config = self.load(self.config_path)  # settings saved meanwhile apply now

        def log(line: str) -> None:
            queue.append_log(conn, job.id, line)
            queue.set_state(conn, {"heartbeat": "1"}, now=self.deps.now())

        def stop() -> bool:
            return queue.stop_requested(conn, job.id)

        log(f"▶ {job.title}   $ {job.cli}")
        try:
            if stop():
                queue.finish(conn, job.id, stopped=True, now=self.deps.now())
                log("⏹ stopped before it started")
            else:
                result = run_kind(conn, config, self.deps, job.kind, job.params, log=log, stop=stop)
                stopped = bool(getattr(result, "stopped", False))
                queue.finish(conn, job.id, result=jsonable(result), stopped=stopped, now=self.deps.now())
        except Exception as exc:  # noqa: BLE001 - every failure must end the job visibly
            if conn.in_transaction:
                conn.rollback()
            queue.finish(conn, job.id, error=str(exc) or exc.__class__.__name__, now=self.deps.now())
            log(f"✗ {exc}")
        queue.purge(conn)
        return queue.get(conn, job.id) or job

    def serve(self, poll: float = 1.0, *, install_signals: bool = True) -> int:
        """Run until SIGTERM/SIGINT; a running job finishes first (Docker waits its grace period)."""
        if install_signals:
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_a: setattr(self, "stop_serving", True))
        self.start()
        while not self.stop_serving:
            self.run_once()
            if self.stop_serving:
                break
            self.deps.sleep(poll)
        return 0


def status_lines(conn: sqlite3.Connection, now=None) -> list[str]:
    """``wristpull agent --status``: heartbeat, devices, the current or last job."""
    state = queue.device_state(conn, now=now)
    lines = [
        "agent:      " + ("online" if state["online"] else "offline") + (f" (last seen {state['last_seen_utc']})" if state["last_seen_utc"] else " (never seen)"),
        "watch:      " + ("attached" if state["watch_present"] else "not attached"),
        "phone:      " + ("attached" if state["phone_present"] else "not attached"),
    ]
    job = queue.current(conn)
    if job is None:
        lines.append("job:        none yet")
    else:
        lines.append(f"job:        #{job.id} {job.kind} {job.status}" + (f": {job.error}" if job.error else "") + f"  $ {job.cli}")
    return lines


