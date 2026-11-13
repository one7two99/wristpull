"""Injectable side effects of the agent (the devices, the clock, sysfs); tests pass fakes."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ..adb import SYSFS_USB, Adb


def real_adb() -> Adb:
    return Adb(binary="adb")


@dataclass
class AgentDeps:
    adb_factory: Callable[[], Any] = real_adb
    mtp_factory: Callable[[], Any] | None = None  # None: wristpull.mtp.MtpTools; tests inject FakeMtp
    transport_factory: Callable[[str], Any] | None = None  # None: bleak; tests inject FakeTransport
    sleep: Callable[[float], None] = time.sleep
    today: Callable[[], date] = date.today
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    sysfs: Path = SYSFS_USB  # where the USB presence is read from
