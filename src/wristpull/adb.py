"""Small synchronous wrapper around the adb command-line tool.

Modelled on the asyncio wrapper in the user's ``kompakt`` project, reduced to
what wristpull needs: list devices, run shell commands, pull files, broadcast
intents. Tests substitute :class:`tests.fake_adb.FakeAdb`.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

_NO_DEVICE_MARKERS = (
    "no devices/emulators found",
    "device not found",
    "device offline",
    "device unauthorized",
    "device still authorizing",
    "more than one device",
)


class AdbError(RuntimeError):
    """adb reported an error."""

    def __init__(self, message: str, *, returncode: int | None = None, stderr: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class NoDeviceError(AdbError):
    """No (authorized) device connected."""


@dataclass(frozen=True)
class Device:
    serial: str
    state: str  # device | offline | unauthorized | ...


def parse_devices(text: str) -> list[Device]:
    devices: list[Device] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            devices.append(Device(parts[0], parts[1]))
    return devices


class Adb:
    def __init__(self, serial: str | None = None, binary: str = "adb") -> None:
        self.serial = serial
        self.binary = binary

    def _argv(self, *args: str) -> list[str]:
        argv = [self.binary]
        if self.serial:
            argv += ["-s", self.serial]
        return argv + list(args)

    def run(self, *args: str, timeout: float = 60.0, check: bool = True) -> str:
        try:
            proc = subprocess.run(self._argv(*args), capture_output=True, text=True, timeout=timeout, check=False)
        except FileNotFoundError as exc:
            raise AdbError(f"adb binary not found: {self.binary}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"adb {' '.join(args)}: timed out after {timeout:.0f} s") from exc
        if check and proc.returncode != 0:
            message = proc.stderr.strip() or proc.stdout.strip() or f"adb exited with code {proc.returncode}"
            if any(marker in message for marker in _NO_DEVICE_MARKERS):
                raise NoDeviceError(message, returncode=proc.returncode, stderr=proc.stderr)
            raise AdbError(message, returncode=proc.returncode, stderr=proc.stderr)
        return proc.stdout.replace("\r\n", "\n")

    def shell(self, command: str, *, timeout: float = 60.0, check: bool = True) -> str:
        return self.run("shell", command, timeout=timeout, check=check)

    def devices(self) -> list[Device]:
        return parse_devices(self.run("devices", timeout=15, check=False))

    def pull(self, remote: str, local: Path, *, timeout: float = 900.0) -> str:
        return self.run("pull", remote, str(local), timeout=timeout)

    def broadcast(self, action: str, *, timeout: float = 30.0) -> str:
        return self.shell(f"am broadcast -a {shlex.quote(action)}", timeout=timeout)

    # -- helpers built on shell -------------------------------------------------

    def file_hashes(self, directory: str) -> dict[str, str]:
        """sha256 of every regular file below ``directory`` (device path -> hex digest)."""
        out = self.shell(
            f"cd {shlex.quote(directory)} 2>/dev/null && find . -type f -exec sha256sum {{}} + 2>/dev/null; true",
            timeout=300,
        )
        hashes: dict[str, str] = {}
        for line in out.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and len(parts[0]) == 64:
                rel = parts[1].strip()
                rel = rel[2:] if rel.startswith("./") else rel
                hashes[f"{directory.rstrip('/')}/{rel}"] = parts[0]
        return hashes

    def stat_mtime(self, path: str) -> int | None:
        """Modification time (Unix seconds) of a device file, or None if missing."""
        out = self.shell(f"stat -c %Y {shlex.quote(path)} 2>/dev/null; true").strip()
        return int(out) if out.isdigit() else None
