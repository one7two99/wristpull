"""One view of a job for the templates, whether it runs on the device agent or in this process.

The agent's jobs come from ``agent.queue`` (rows with a JSON result), the coach runs from
``web/jobs.py`` (in-memory objects); both end up as a :class:`JobView` with the same fields and the
summary lines of ``jobtext.describe_result``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..agent.queue import AgentJob
from ..jobtext import Gettext, JobRecord, JobSpec, Ngettext, describe_result, result_from_json
from .jobs import Job

COACH_TITLES = {"daily": "Coach: daily check", "weekly": "Coach: weekly review", "ask": "Coach: question"}


@dataclass
class JobView:
    id: int
    source: str  # agent | web
    kind: str
    title: str  # msgid
    status: str  # queued | running | done | error | stopped
    seconds: int
    cli: str = ""
    error: str | None = None
    lines: list[str] = field(default_factory=list)  # summary lines of a finished job
    log: list[str] = field(default_factory=list)
    can_stop: bool = False
    device: str = ""

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def active(self) -> bool:
        return self.status in ("queued", "running")

    @property
    def finished(self) -> bool:
        return not self.active


def from_agent(job: AgentJob, *, log: list[str] | None = None, gettext: Gettext, ngettext: Ngettext, now: datetime | None = None) -> JobView:
    from ..agent.kinds import KINDS

    lines: list[str] = []
    if job.status in ("done", "stopped", "error"):
        record = JobRecord(JobSpec(job.kind, gettext(job.title), job.cli, None), result=result_from_json(job.result) if job.result is not None else None,
                           error=RuntimeError(job.error) if job.error else None, cancelled=job.status == "stopped")
        lines = describe_result(record, gettext=gettext, ngettext=ngettext)
    spec = KINDS.get(job.kind)
    return JobView(
        id=job.id,
        source="agent",
        kind=job.kind,
        title=job.title,
        status=job.status,
        seconds=job.seconds(now),
        cli=job.cli,
        error=job.error,
        lines=lines,
        log=list(log or []),
        can_stop=job.active and job.stoppable and not job.stop_requested,
        device=spec.device if spec else "",
    )


def from_web(job: Job) -> JobView:
    """A coach run of this process (``web/jobs.py``)."""
    kind = job.key.removeprefix("coach-")
    lines = []
    if job.status == "done" and isinstance(job.result, dict):
        lines = [f"  ! {w}" for w in job.result.get("warnings", [])]
    return JobView(
        id=job.id,
        source="web",
        kind=job.key,
        title=COACH_TITLES.get(kind, job.title),
        status=job.status,
        seconds=job.seconds,
        error=job.error,
        lines=lines,
        log=list(job.log),
    )


def empty() -> None:
    return None


def as_rows(views: list[JobView]) -> list[dict[str, Any]]:  # pragma: no cover - trivial
    return [vars(v) for v in views]
