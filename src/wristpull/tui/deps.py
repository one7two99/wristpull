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

from ..adb import Adb
from ..garmin_usb import gio_garmin_mtp_urls, try_mount_garmin


def open_with_desktop(path: Path) -> None:
    """Open a file or directory with the desktop's default handler (xdg-open), detached."""
    subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def real_adb() -> Adb:
    return Adb(binary="adb")


@dataclass
class TuiDeps:
    adb_factory: Callable[[], Adb] = real_adb
    roots: Callable[[], list[Path]] | None = None  # None: the system mount points
    mounter: Callable[[], tuple[bool, str]] = try_mount_garmin
    mtp_urls: Callable[[], list[str]] = gio_garmin_mtp_urls  # attached-but-unmounted Garmin devices
    sleep: Callable[[float], None] = time.sleep
    today: Callable[[], date] = date.today
    open_path: Callable[[Path], None] = open_with_desktop
    refresh_interval: float = 30.0  # seconds between device polls on the overview; 0 disables
    opened: list[Path] = field(default_factory=list)  # recorder used by tests and the files screen
