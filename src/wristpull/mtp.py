"""The watch over MTP without a mount: libmtp's command-line tools behind a small interface.

Inside the Docker container there is no gvfs, so the fenix is reached with ``mtp-tools``
(``mtp-detect``, ``mtp-folders``, ``mtp-files``, ``mtp-getfile``, ``mtp-sendfile``,
``mtp-delfile``). Every call opens and enumerates the device again (about six seconds on a
fenix 6X Pro), so callers take one snapshot per command and fetch files by id afterwards.
The tools' exit codes carry no information ("no device" is rc 0); the text decides, see
``tests/fixtures/mtp/`` for verbatim captures. The implementation behind :class:`Mtp` can be
swapped for a ctypes binding to libmtp without touching callers or :class:`tests.fake_mtp.FakeMtp`.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import Config
from .garmin_usb import Runner
from .garmin_usb import run_command as _run_command
from .runtime import in_container

MTP_TIMEOUT = 300.0  # mtp-files walks every object on the device
GARMIN_ROOT = "/GARMIN"
MTP_TOOLS_HINT = "mtp-tools (libmtp) are not installed: apt install mtp-tools, or use the Docker image"
MTP_BUSY_HINT = (
    "another program holds the watch (a gvfs mount, Dolphin or kiod6 on the host, or no permission on "
    "/dev/bus/usb); unmount it there or see README 'Running everything in Docker'"
)
NOISE = "MTP extended association type"
_NO_DEVICE = re.compile(r"no (raw )?devices?( have been)? found|^No devices\.$", re.IGNORECASE | re.MULTILINE)
_BUSY = ("libusb_claim_interface", "Unable to open raw device", "libusb_open() failed", "LIBMTP PANIC: Unable to initialize device")
_FOLDER_LINE = re.compile(r"^(\d+)\t( *)(.*?)\s*$")
_FILE_BLOCK = re.compile(r"^File ID: (\d+)\n\s+Filename: (.*)\n\s+File size (\d+) .*\n\s+Parent ID: (\d+)", re.MULTILINE)
_DETECT_LINE = re.compile(r"^\s+(Manufacturer|Model|Serial number|Device version|StorageDescription): (.*?)\s*$", re.MULTILINE)


class MtpError(RuntimeError):
    """An MTP tool failed or answered unexpectedly."""


class NoMtpDeviceError(MtpError):
    """No MTP device is attached."""


class MtpBusyError(MtpError):
    """The device is attached but cannot be opened (held by another program, or no permission)."""


@dataclass(frozen=True)
class MtpDeviceInfo:
    vendor: str | None
    model: str | None
    serial: str | None  # the USB serial, not the Garmin unit id (that one is in GarminDevice.xml)
    version: str | None
    storages: tuple[str, ...]

    def label(self) -> str:
        return f"{self.model or 'MTP device'}" + (f" ({self.serial})" if self.serial else "")


@dataclass(frozen=True)
class MtpFile:
    id: int
    name: str
    size: int
    parent_id: int
    path: str  # "/GARMIN/Monitor/MB700000.FIT"


class Mtp(Protocol):
    def device_info(self) -> MtpDeviceInfo: ...
    def folders(self) -> dict[int, str]: ...
    def list(self, folder_path: str) -> list[MtpFile]: ...
    def get(self, file_id: int, dest: Path) -> None: ...
    def send(self, src: Path, folder_path: str, name: str) -> int: ...
    def delete(self, file_id: int) -> None: ...
    def invalidate(self) -> None: ...


# --- parsers (pure) -------------------------------------------------------------------------


def parse_folders(text: str) -> dict[int, str]:
    """``mtp-folders`` output -> {id: "/GARMIN/Monitor"}; lines are ``<id>\\t<two spaces per level><name>``."""
    folders: dict[int, str] = {}
    stack: list[str] = []
    for line in text.splitlines():
        m = _FOLDER_LINE.match(line)
        if not m:
            continue
        level = len(m.group(2)) // 2
        del stack[level:]
        stack.append(m.group(3))
        folders[int(m.group(1))] = "/" + "/".join(stack)
    return folders


def parse_files(text: str, folders: Mapping[int, str]) -> list[MtpFile]:
    """``mtp-files`` output -> files with their device path (parent 0 or unknown parents sit at the root)."""
    files: list[MtpFile] = []
    for m in _FILE_BLOCK.finditer(text):
        parent = int(m.group(4))
        name = m.group(2).strip()
        files.append(MtpFile(id=int(m.group(1)), name=name, size=int(m.group(3)), parent_id=parent, path=f"{folders.get(parent, '')}/{name}"))
    return files


def parse_detect(text: str) -> MtpDeviceInfo:
    """``mtp-detect`` output -> device info (the first value of every label wins, storages collect)."""
    values: dict[str, str] = {}
    storages: list[str] = []
    for m in _DETECT_LINE.finditer(text):
        if m.group(1) == "StorageDescription":
            storages.append(m.group(2))
        else:
            values.setdefault(m.group(1), m.group(2))
    return MtpDeviceInfo(vendor=values.get("Manufacturer"), model=values.get("Model"), serial=values.get("Serial number"), version=values.get("Device version"), storages=tuple(storages))


def no_device(text: str) -> bool:
    return _NO_DEVICE.search(text) is not None


def busy(text: str) -> bool:
    return any(marker in text for marker in _BUSY)


def folder_id(folders: Mapping[int, str], folder_path: str) -> tuple[int, str] | None:
    """``(id, path as the device spells it)`` for a path compared case-insensitively, or None."""
    wanted = folder_path.strip("/").lower()
    for fid, path in folders.items():
        if path.strip("/").lower() == wanted:
            return fid, path
    return None


# --- the tools ------------------------------------------------------------------------------


def run_command(argv: list[str]) -> tuple[int, str, str]:
    """Default runner (the conftest guard replaces this attribute; the tools are looked up at call time)."""
    return _run_command(argv, timeout=MTP_TIMEOUT)


class MtpTools:
    """``Mtp`` on top of the libmtp command-line tools; one device snapshot per instance."""

    def __init__(self, runner: Runner | None = None) -> None:
        self._runner = runner
        self._info: MtpDeviceInfo | None = None
        self._snapshot: tuple[dict[int, str], list[MtpFile]] | None = None

    def _run(self, *argv: str) -> tuple[int, str, str]:
        code, out, err = (self._runner or run_command)(list(argv))
        if code == 127:
            raise MtpError(f"{argv[0]}: {MTP_TOOLS_HINT} ({err.strip() or 'not found'})")
        if busy(err) or busy(out):
            raise MtpBusyError(MTP_BUSY_HINT)
        if no_device(out) or no_device(err):
            raise NoMtpDeviceError("no MTP device found (is the watch plugged in and awake?)")
        return code, out, err

    def device_info(self) -> MtpDeviceInfo:
        if self._info is None:
            _code, out, _err = self._run("mtp-detect")
            self._info = parse_detect(out)
            if self._info.model is None and self._info.serial is None:
                raise MtpError("mtp-detect found a device but reported no model or serial")
        return self._info

    def _take_snapshot(self) -> tuple[dict[int, str], list[MtpFile]]:
        if self._snapshot is None:
            _code, out, _err = self._run("mtp-folders")
            folders = parse_folders(out)
            if not folders:
                raise MtpError("mtp-folders listed no folders (device not opened?)")
            _code, out, _err = self._run("mtp-files")
            self._snapshot = (folders, parse_files(out, folders))
        return self._snapshot

    def folders(self) -> dict[int, str]:
        return dict(self._take_snapshot()[0])

    def list(self, folder_path: str) -> list[MtpFile]:
        folders, files = self._take_snapshot()
        hit = folder_id(folders, folder_path)
        if hit is None:
            return []
        return sorted((f for f in files if f.parent_id == hit[0]), key=lambda f: f.name.lower())

    def get(self, file_id: int, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        code, out, err = self._run("mtp-getfile", str(file_id), str(dest))
        if code != 0 or not dest.exists():
            dest.unlink(missing_ok=True)
            raise MtpError(f"mtp-getfile {file_id}: {_tail(out, err) or f'exit code {code}'}")
        expected = next((f.size for f in self._take_snapshot()[1] if f.id == file_id), None) if self._snapshot else None
        if expected is not None and dest.stat().st_size != expected:
            actual = dest.stat().st_size
            dest.unlink(missing_ok=True)
            raise MtpError(f"mtp-getfile {file_id}: got {actual} bytes, the listing said {expected}")

    def send(self, src: Path, folder_path: str, name: str) -> int:
        """Copy ``src`` to ``<folder_path>/<name>`` (the tool names the file after the local one, so stage it)."""
        hit = folder_id(self._take_snapshot()[0], folder_path)
        if hit is None:
            raise MtpError(f"no folder {folder_path} on the device")
        with tempfile.TemporaryDirectory(prefix="wristpull-mtp-") as tmp:
            staged = Path(tmp) / name
            shutil.copyfile(src, staged)
            code, out, err = self._run("mtp-sendfile", str(staged), hit[1])
        self.invalidate()
        m = re.search(r"New file ID: (\d+)", out)
        if code != 0 or m is None:
            raise MtpError(f"mtp-sendfile {name} to {hit[1]}: {_tail(out, err) or f'exit code {code}'}")
        return int(m.group(1))

    def delete(self, file_id: int) -> None:
        code, out, err = self._run("mtp-delfile", "-n", str(file_id))
        self.invalidate()
        if code != 0 or "Failed to" in out or "Failed to" in err:
            raise MtpError(f"mtp-delfile {file_id}: {_tail(out, err) or f'exit code {code}'}")

    def invalidate(self) -> None:
        self._snapshot = None


def _tail(out: str, err: str) -> str:
    """The last meaningful line of the tools' chatter."""
    lines = [line.strip() for line in (err + "\n" + out).splitlines() if line.strip() and NOISE not in line and "Progress:" not in line]
    lines = [line for line in lines if not line.startswith(("Device 0 (", "libmtp version", "Attempting to connect", "Your system does not", "If you want", "please switch"))]
    return lines[-1] if lines else ""


# --- choosing the access path ---------------------------------------------------------------


def resolve_access(config: Config, env: Mapping[str, str] | None = None) -> str:
    """``[garmin] access`` with ``auto`` resolved: MTP tools inside the container, gvfs/mount otherwise."""
    env = os.environ if env is None else env
    access = config.garmin_access
    if access == "auto":
        return "mtp" if in_container(env) else "gvfs"
    return access


def probe_mtp(mtp: Mtp) -> tuple[MtpDeviceInfo | None, str]:
    """``(info, message)``: the device when one answers, otherwise None and the reason."""
    try:
        info = mtp.device_info()
    except NoMtpDeviceError as exc:
        return None, str(exc)
    except MtpBusyError:
        return None, MTP_BUSY_HINT
    except MtpError as exc:
        return None, str(exc)
    return info, f"MTP: {info.label()}"


def mtp_device_present(runner: Runner | None = None) -> bool:
    return probe_mtp(MtpTools(runner))[0] is not None


MtpFactory = Callable[[], Mtp]
