"""The job queue and the agent's state in the shared SQLite database (the only SQL of the agent).

The web app enqueues, the agent claims and finishes; both poll the same rows. One job runs at a
time: ``enqueue`` refuses while a job is queued or running. ``request_stop`` is the cooperative
stop flag; the runner hands the library a callback that reads it between files.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..timeutil import iso_utc
from .kinds import KINDS, validate

ACTIVE = ("queued", "running")
FINAL = ("done", "error", "stopped")
LOG_CAP = 2000  # lines kept per job
HEARTBEAT_MAX_AGE_S = 15  # the agent writes every second; beyond this it counts as offline
RUNNING_GRACE_S = 600  # while a job runs, a heartbeat may age this much before "offline" (long write transactions)
KEEP_JOBS = 50


@dataclass
class AgentJob:
    id: int
    kind: str
    params: dict[str, Any]
    title: str
    cli: str
    status: str
    stop_requested: bool
    requested_utc: str
    started_utc: str | None = None
    finished_utc: str | None = None
    result: Any = None  # JSON data as stored
    error: str | None = None
    log: list[str] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    @property
    def stoppable(self) -> bool:
        spec = KINDS.get(self.kind)
        return bool(spec and spec.stoppable)

    def seconds(self, now: datetime | None = None) -> int:
        """Run time so far, or the final duration."""
        start = _parse(self.started_utc) or _parse(self.requested_utc)
        end = _parse(self.finished_utc) if self.finished_utc else (now or datetime.now(UTC))
        if start is None or end is None:
            return 0
        return max(0, int((end - start).total_seconds()))


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _now(now: datetime | None) -> str:
    return iso_utc(now or datetime.now(UTC))


def _row_to_job(row: sqlite3.Row) -> AgentJob:
    return AgentJob(
        id=int(row["id"]),
        kind=str(row["kind"]),
        params=json.loads(row["params"]),
        title=str(row["title"]),
        cli=str(row["cli"]),
        status=str(row["status"]),
        stop_requested=bool(row["stop_requested"]),
        requested_utc=str(row["requested_utc"]),
        started_utc=row["started_utc"],
        finished_utc=row["finished_utc"],
        result=json.loads(row["result"]) if row["result"] else None,
        error=row["error"],
    )


def enqueue(conn: sqlite3.Connection, kind: str, params: dict[str, Any] | None = None, *, now: datetime | None = None) -> AgentJob | None:
    """Store a job for the agent; ``None`` while another job is queued or running. Raises ``ValueError``
    for an unknown kind or bad parameters (validated before anything is written)."""
    clean = validate(kind, params or {})
    spec = KINDS[kind]
    if current_active(conn) is not None:
        return None
    cur = conn.execute(
        "INSERT INTO agent_jobs (kind, params, title, cli, status, requested_utc) VALUES (?, ?, ?, ?, 'queued', ?)",
        (kind, json.dumps(clean, separators=(",", ":")), spec.title, spec.cli(clean), _now(now)),
    )
    conn.commit()
    return get(conn, int(cur.lastrowid))


def get(conn: sqlite3.Connection, job_id: int) -> AgentJob | None:
    row = conn.execute("SELECT * FROM agent_jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def current_active(conn: sqlite3.Connection) -> AgentJob | None:
    """The running job, else the oldest queued one."""
    row = conn.execute("SELECT * FROM agent_jobs WHERE status IN ('queued', 'running') ORDER BY status = 'running' DESC, id LIMIT 1").fetchone()
    return _row_to_job(row) if row else None


def current(conn: sqlite3.Connection) -> AgentJob | None:
    """The active job, else the most recent finished one."""
    job = current_active(conn)
    if job is not None:
        return job
    row = conn.execute("SELECT * FROM agent_jobs ORDER BY id DESC LIMIT 1").fetchone()
    return _row_to_job(row) if row else None


def recent(conn: sqlite3.Connection, limit: int = 20) -> list[AgentJob]:
    rows = conn.execute("SELECT * FROM agent_jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_row_to_job(r) for r in rows]


def claim(conn: sqlite3.Connection, *, now: datetime | None = None) -> AgentJob | None:
    """Mark the oldest queued job as running and return it (``None`` when nothing waits or one runs)."""
    if conn.execute("SELECT 1 FROM agent_jobs WHERE status = 'running' LIMIT 1").fetchone():
        return None
    row = conn.execute("SELECT id FROM agent_jobs WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
    if row is None:
        return None
    conn.execute("UPDATE agent_jobs SET status = 'running', started_utc = ? WHERE id = ? AND status = 'queued'", (_now(now), row[0]))
    conn.commit()
    return get(conn, int(row[0]))


def append_log(conn: sqlite3.Connection, job_id: int, line: str, *, cap: int = LOG_CAP) -> None:
    """Add one log line (kept to the last ``cap``); a line written from inside an importer's transaction is
    not committed here but lands together with that file's rows."""
    inside = conn.in_transaction
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM agent_job_log WHERE job_id = ?", (job_id,)).fetchone()[0]
    conn.execute("INSERT INTO agent_job_log (job_id, seq, line) VALUES (?, ?, ?)", (job_id, seq, line.rstrip("\n")))
    if seq > cap:
        conn.execute("DELETE FROM agent_job_log WHERE job_id = ? AND seq <= ?", (job_id, seq - cap))
    if not inside:
        conn.commit()


def log_lines(conn: sqlite3.Connection, job_id: int, limit: int = 200) -> list[str]:
    rows = conn.execute("SELECT line FROM agent_job_log WHERE job_id = ? ORDER BY seq DESC LIMIT ?", (job_id, limit)).fetchall()
    return [str(r[0]) for r in reversed(rows)]


def finish(conn: sqlite3.Connection, job_id: int, *, result: Any = None, error: str | None = None, stopped: bool = False, now: datetime | None = None) -> None:
    status = "error" if error else ("stopped" if stopped else "done")
    conn.execute(
        "UPDATE agent_jobs SET status = ?, finished_utc = ?, result = ?, error = ? WHERE id = ?",
        (status, _now(now), json.dumps(result, separators=(",", ":")) if result is not None else None, error, job_id),
    )
    conn.commit()


def request_stop(conn: sqlite3.Connection, job_id: int) -> bool:
    """Ask the running (or queued) job to stop; False when it is not active."""
    cur = conn.execute("UPDATE agent_jobs SET stop_requested = 1 WHERE id = ? AND status IN ('queued', 'running')", (job_id,))
    conn.commit()
    return cur.rowcount > 0


def stop_requested(conn: sqlite3.Connection, job_id: int) -> bool:
    row = conn.execute("SELECT stop_requested FROM agent_jobs WHERE id = ?", (job_id,)).fetchone()
    return bool(row and row[0])


def fail_stale(conn: sqlite3.Connection, message: str, *, now: datetime | None = None) -> list[int]:
    """Jobs still marked running from an agent that died: mark them as errors (called at agent start)."""
    ids = [int(r[0]) for r in conn.execute("SELECT id FROM agent_jobs WHERE status = 'running'").fetchall()]
    for job_id in ids:
        finish(conn, job_id, error=message, now=now)
    return ids


def purge(conn: sqlite3.Connection, keep: int = KEEP_JOBS) -> int:
    """Drop finished jobs beyond the newest ``keep`` (their log goes with them)."""
    cur = conn.execute(
        "DELETE FROM agent_jobs WHERE status IN ('done', 'error', 'stopped') AND id NOT IN (SELECT id FROM agent_jobs ORDER BY id DESC LIMIT ?)",
        (keep,),
    )
    conn.commit()
    return cur.rowcount


# ---- agent state ---------------------------------------------------------------------------------


def set_state(conn: sqlite3.Connection, values: dict[str, str], *, now: datetime | None = None) -> None:
    inside = conn.in_transaction  # written from a job's log callback: commits with the importer's transaction
    stamp = _now(now)
    conn.executemany("INSERT OR REPLACE INTO agent_state (key, value, updated_utc) VALUES (?, ?, ?)", [(k, v, stamp) for k, v in values.items()])
    if not inside:
        conn.commit()


def pulse(db_path, *, now: datetime | None = None, wait_s: float = 0.5) -> bool:
    """The heartbeat alone, on a short-lived connection of its own (the agent's side thread while a job
    runs); False when the database was busy or the write failed, which the next second repairs."""
    try:
        with closing(sqlite3.connect(str(db_path), timeout=wait_s)) as conn:
            conn.execute("INSERT OR REPLACE INTO agent_state (key, value, updated_utc) VALUES ('heartbeat', '1', ?)", (_now(now),))
            conn.commit()
        return True
    except sqlite3.Error:
        return False


def get_state(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """``key -> (value, updated_utc)``."""
    return {str(r[0]): (str(r[1]), str(r[2])) for r in conn.execute("SELECT key, value, updated_utc FROM agent_state").fetchall()}


def device_state(conn: sqlite3.Connection, *, now: datetime | None = None, max_age_s: int = HEARTBEAT_MAX_AGE_S) -> dict[str, Any]:
    """What the dashboard shows: agent online (heartbeat age), watch and phone attached, the agent's version."""
    state = get_state(conn)
    now = now or datetime.now(UTC)
    heartbeat = state.get("heartbeat")
    age = None
    if heartbeat:
        when = _parse(heartbeat[1])
        age = int((now - when).total_seconds()) if when else None
    online = age is not None and 0 <= age <= max_age_s
    if not online and age is not None and 0 <= age <= RUNNING_GRACE_S:
        running = conn.execute("SELECT 1 FROM agent_jobs WHERE status = 'running' LIMIT 1").fetchone()
        online = running is not None
    bluetooth = state.get("bluetooth_ready")
    return {
        "online": online,
        "heartbeat_age_s": age,
        "last_seen_utc": heartbeat[1] if heartbeat else None,
        "watch_present": online and state.get("watch_present", ("0", ""))[0] == "1",
        "phone_present": online and state.get("phone_present", ("0", ""))[0] == "1",
        "bluetooth_ready": (bluetooth[0] == "1") if (online and bluetooth) else None,  # None: unknown
        "version": state.get("agent_version", ("", ""))[0] or None,
    }
