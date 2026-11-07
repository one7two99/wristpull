"""Small synchronous wrapper around the adb command-line tool.

Modelled on the asyncio wrapper in the user's ``kompakt`` project, reduced to
what wristpull needs: list devices, run shell commands, pull files, broadcast
intents. Tests substitute :class:`tests.fake_adb.FakeAdb`.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

SYSFS_USB = Path("/sys/bus/usb/devices")
_WIRELESS_SERIAL = re.compile(r"^[A-Za-z0-9.\-\[\]:]+:\d{1,5}$")
HOST_ADB_HINT = (
    "another adb server (on the host?) may hold the phone: run `adb kill-server` on the host, "
    "or accept the USB debugging prompt on the phone"
)

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


def is_wireless_serial(serial: str | None) -> bool:
    """``host:port`` serials name a phone reached over wireless debugging (``adb connect``)."""
    return bool(serial) and _WIRELESS_SERIAL.match(serial) is not None and not serial.startswith("emulator-")


def adb_usb_devices(sysfs: Path = SYSFS_USB) -> list[str]:
    """``vendor:product`` of attached USB devices that expose an ADB interface (class ff, subclass 42, protocol 01).

    Used to tell "no phone" from "a phone is attached but this adb server cannot see it"; reads sysfs only.
    """
    found: list[str] = []
    if not sysfs.is_dir():
        return found
    for dev in sorted(sysfs.iterdir()):
        vendor, product = dev / "idVendor", dev / "idProduct"
        if not vendor.is_file() or not product.is_file():
            continue
        for iface in dev.parent.glob(f"{dev.name}:*"):
            try:
                klass = (iface / "bInterfaceClass").read_text().strip().lower()
                sub = (iface / "bInterfaceSubClass").read_text().strip().lower()
                proto = (iface / "bInterfaceProtocol").read_text().strip().lower()
            except OSError:
                continue
            if (klass, sub, proto) == ("ff", "42", "01"):
                found.append(f"{vendor.read_text().strip()}:{product.read_text().strip()}")
                break
    return found


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

    def connect(self, address: str, *, timeout: float = 30.0) -> str:
        """``adb connect host:port`` (wireless debugging); adb exits 0 even when it fails, so the text decides."""
        out = self.run("connect", address, timeout=timeout, check=False).strip()
        if "connected to" not in out:
            raise NoDeviceError(out or f"adb connect {address}: no answer")
        return out

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
