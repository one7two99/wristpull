"""Injectable side effects of the TUI: devices, clock, sleeping, opening files.

The CLI passes the real defaults; tests pass fakes so the UI never touches hardware.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ..adb import Adb
from ..runtime import host_path, in_container
from .i18n import _

Opener = Callable[[Path], str | None]  # returns a message to show instead of "Opened ...", or None


def open_with_desktop(path: Path) -> str | None:
    """Open a file or directory with the desktop's default handler (xdg-open), detached."""
    subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return None


def open_by_notice(path: Path) -> str:
    """No desktop here (container, headless): tell the user where the file is on the host."""
    return _("No desktop here; open on the host: {path}").format(path=host_path(path))


def open_path_default(path: Path) -> str | None:
    """xdg-open where there is a desktop; in the container, or without xdg-open, a notice with the host path."""
    if in_container():
        return open_by_notice(path)
    try:
        return open_with_desktop(path)
    except FileNotFoundError:
        return open_by_notice(path)


def real_adb() -> Adb:
    return Adb(binary="adb")


@dataclass
class TuiDeps:
    adb_factory: Callable[[], Adb] = real_adb
    sleep: Callable[[float], None] = time.sleep
    today: Callable[[], date] = date.today
    open_path: Opener = open_path_default
    refresh_interval: float = 30.0  # seconds between device polls on the overview; 0 disables
    coach_backend: Callable[..., Any] | None = None  # tests inject a FakeBackend factory (config, **kwargs) -> Backend
    editor: Callable[[Path], None] | None = None  # None: $EDITOR in a suspended terminal; tests inject a recorder
    mtp_factory: Callable[[], Any] | None = None  # None: wristpull.mtp.MtpTools; tests inject FakeMtp
    transport_factory: Callable[[str], Any] | None = None  # None: bleak (zeppos.transport.open_transport); tests inject FakeTransport
    opened: list[Path] = field(default_factory=list)  # recorder used by tests and the files screen
