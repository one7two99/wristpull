"""Shared helpers for the TUI tests: run coroutines, build an app with fakes."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

from fake_adb import FakeAdb
from wristpull.config import Config
from wristpull.tui.deps import TuiDeps

TODAY = date(2026, 10, 6)


def run(coro):
    return asyncio.run(coro)


def make_deps(tmp_path: Path, garmin_tree: Path | None = None, *, phone_root: Path | None = None) -> TuiDeps:
    opened: list[Path] = []
    return TuiDeps(
        adb_factory=lambda: FakeAdb(phone_root or tmp_path / "phone"),
        roots=(lambda: [garmin_tree]) if garmin_tree else (lambda: []),
        mounter=lambda: (False, "no mounter in tests"),
        mtp_urls=lambda: [],
        remounter=lambda roots: (False, "no remounter in tests"),
        sleep=lambda _s: None,
        today=lambda: TODAY,
        open_path=opened.append,
        refresh_interval=0,
    )


def make_app(config: Config, deps: TuiDeps):
    from wristpull.tui.app import WristpullApp

    return WristpullApp(config, deps)


async def wait_jobs(app) -> None:
    """Wait for the job workers; a failed worker is reported by the runner, not raised here."""
    from textual.worker import WorkerFailed

    try:
        await app.workers.wait_for_complete()
    except WorkerFailed:
        pass
