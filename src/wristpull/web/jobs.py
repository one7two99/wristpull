"""Background jobs of the web dashboard (coach runs): one at a time, each with its own SQLite connection.

The registry lives in process memory – the reason the server runs exactly one worker: a status poll
served by another worker would not see the job. Pattern from ``tui/jobs.py`` (which needs Textual).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..db import connect

Log = Callable[[str], None]
MAX_LOG_LINES = 200


@dataclass
class Job:
    id: int
    key: str
    title: str
    status: str = "running"  # running | done | error
    cli: str = ""  # the equivalent command line, shown with the result
    log: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: float | None = None
    result: Any = None
    error: str | None = None

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def seconds(self) -> int:
        return int((self.finished or time.time()) - self.started)


class JobRegistry:
    """Starts jobs in a daemon thread (``threaded=False`` runs them inline, for tests)."""

    def __init__(self, db_path: Path, *, threaded: bool = True):
        self.db_path = db_path
        self.threaded = threaded
        self._lock = threading.Lock()
        self._jobs: list[Job] = []

    def current(self) -> Job | None:
        """The running job, else the most recent one."""
        with self._lock:
            return self._jobs[-1] if self._jobs else None

    def last_of(self, keys: tuple[str, ...]) -> Job | None:
        """The most recent job whose key is one of ``keys`` (running or finished)."""
        with self._lock:
            return next((j for j in reversed(self._jobs) if j.key in keys), None)

    def get(self, job_id: int) -> Job | None:
        with self._lock:
            return next((j for j in self._jobs if j.id == job_id), None)

    def start(self, key: str, title: str, fn: Callable[[sqlite3.Connection, Log], Any], *, cli: str = "") -> Job | None:
        """Start ``fn(conn, log)``; None while another job is still running."""
        with self._lock:
            if self._jobs and self._jobs[-1].running:
                return None
            job = Job(id=len(self._jobs) + 1, key=key, title=title, cli=cli)
            self._jobs = (self._jobs + [job])[-20:]
        if self.threaded:
            threading.Thread(target=self._work, args=(job, fn), name=f"wristpull-job-{job.id}", daemon=True).start()
        else:
            self._work(job, fn)
        return job

    def _work(self, job: Job, fn: Callable[[sqlite3.Connection, Log], Any]) -> None:
        def log(line: str) -> None:
            job.log = (job.log + [line.strip()])[-MAX_LOG_LINES:]

        try:
            with closing(connect(self.db_path)) as conn:
                job.result = fn(conn, log)
            job.status = "done"
        except Exception as exc:  # noqa: BLE001 - every failure must end the job visibly
            job.error = str(exc) or exc.__class__.__name__
            job.status = "error"
        finally:
            job.finished = time.time()
