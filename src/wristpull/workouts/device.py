"""The watch's workout folders behind one small interface: a mounted GARMIN directory or the MTP tools.

Push, verify, listing and removal (`push.py`, `watch.py`) only talk to :class:`WatchFiles`, so the
rules that protect the watch (never overwrite, remove only top-level workout files after a backup,
raw-data folders read-only) are written once. :class:`LocalWatch` wraps today's path/gio code,
:class:`MtpWatch` the libmtp tools (:mod:`wristpull.mtp`).
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from ..config import Config
from ..garmin_usb import Runner, eject_garmin, parse_device_info, read_device_info, run_command, try_mount_garmin
from ..importers.garmin import locate_garmin
from ..mtp import GARMIN_ROOT, Mtp, MtpDeviceInfo, MtpError, MtpFactory, MtpTools, folder_id, probe_mtp, resolve_access
from ..timeutil import iso_utc

MTP_ROOT = Path(GARMIN_ROOT)  # the device namespace, shown as the root of an MtpWatch


@dataclass(frozen=True)
class WatchEntry:
    name: str
    size: int
    mtime_utc: str | None  # None over MTP (the tools report no modification time)


class WatchFiles(Protocol):
    root: Path

    def describe(self) -> str: ...
    def unit_id(self) -> str | None: ...
    def folder_path(self, folder: str) -> Path: ...
    def locator(self, folder: str, name: str) -> str: ...
    def list(self, folder: str) -> list[WatchEntry]: ...
    def read(self, folder: str, name: str) -> bytes: ...
    def exists(self, folder: str, name: str) -> bool: ...
    def write(self, folder: str, name: str, data: bytes) -> str: ...
    def remove(self, folder: str, name: str) -> str: ...
    def eject(self) -> tuple[bool, str]: ...


class LocalWatch:
    """A mounted ``GARMIN`` directory (gvfs MTP mount, USB mass storage, or a directory passed with ``--path``)."""

    def __init__(self, root: Path, *, runner: Runner = run_command) -> None:
        self.root = root
        self.runner = runner

    def describe(self) -> str:
        return str(self.root)

    def unit_id(self) -> str | None:
        return read_device_info(self.root).get("unit_id")

    def folder_path(self, folder: str) -> Path:
        """``GARMIN/<folder>`` matching the existing folder's case (the fenix has NewFiles and Workouts)."""
        try:
            for child in self.root.iterdir():
                if child.is_dir() and child.name.lower() == folder.lower():
                    return child
        except OSError:
            pass
        return self.root / folder

    def locator(self, folder: str, name: str) -> str:
        return str(self.folder_path(folder) / name)

    def list(self, folder: str) -> list[WatchEntry]:
        target = self.folder_path(folder)
        if not target.is_dir():
            return []
        entries: list[WatchEntry] = []
        for path in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if not (path.is_file() and path.suffix.lower() == ".fit"):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            entries.append(WatchEntry(path.name, st.st_size, iso_utc(datetime.fromtimestamp(st.st_mtime, UTC))))
        return entries

    def read(self, folder: str, name: str) -> bytes:
        return (self.folder_path(folder) / name).read_bytes()

    def exists(self, folder: str, name: str) -> bool:
        return (self.folder_path(folder) / name).exists()

    def write(self, folder: str, name: str, data: bytes) -> str:
        from .push import write_device_file

        target = self.folder_path(folder)
        if not target.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        if (target / name).exists():
            raise FileExistsError(f"{target / name} exists; files on the watch are never overwritten")
        return write_device_file(target / name, data, runner=self.runner)

    def remove(self, folder: str, name: str) -> str:
        from .watch import remove_device_file

        return remove_device_file(self.folder_path(folder) / name, runner=self.runner)

    def eject(self) -> tuple[bool, str]:
        return eject_garmin(self.root, self.runner)


class MtpWatch:
    """The watch through the libmtp tools: no mount, every operation opens the device (about 6 s each)."""

    def __init__(self, mtp: Mtp, info: MtpDeviceInfo) -> None:
        self.mtp = mtp
        self.info = info
        self.root = MTP_ROOT
        self._unit_id: str | None = None

    def describe(self) -> str:
        return f"mtp://GARMIN on {self.info.label()}"

    def unit_id(self) -> str | None:
        if self._unit_id is None:
            xml = next((f for f in self.mtp.list(GARMIN_ROOT) if f.name.lower() == "garmindevice.xml"), None)
            if xml is not None:
                with tempfile.TemporaryDirectory(prefix="wristpull-mtp-") as tmp:
                    dest = Path(tmp) / xml.name
                    try:
                        self.mtp.get(xml.id, dest)
                        self._unit_id = parse_device_info(dest.read_bytes()).get("unit_id")
                    except (MtpError, OSError):
                        self._unit_id = None
        return self._unit_id

    def _device_folder(self, folder: str) -> str:
        hit = folder_id(self.mtp.folders(), f"{GARMIN_ROOT}/{folder}")
        return hit[1] if hit else f"{GARMIN_ROOT}/{folder}"

    def folder_path(self, folder: str) -> Path:
        return Path(self._device_folder(folder))

    def locator(self, folder: str, name: str) -> str:
        return f"mtp:/{self._device_folder(folder)}/{name}"

    def list(self, folder: str) -> list[WatchEntry]:
        return [WatchEntry(f.name, f.size, None) for f in self.mtp.list(f"{GARMIN_ROOT}/{folder}") if f.name.lower().endswith(".fit")]

    def _entry(self, folder: str, name: str):
        return next((f for f in self.mtp.list(f"{GARMIN_ROOT}/{folder}") if f.name.lower() == name.lower()), None)

    def read(self, folder: str, name: str) -> bytes:
        entry = self._entry(folder, name)
        if entry is None:
            raise FileNotFoundError(f"{GARMIN_ROOT}/{folder}/{name} is not on the watch")
        with tempfile.TemporaryDirectory(prefix="wristpull-mtp-") as tmp:
            dest = Path(tmp) / name
            try:
                self.mtp.get(entry.id, dest)
            except MtpError as exc:
                raise OSError(str(exc)) from exc
            return dest.read_bytes()

    def exists(self, folder: str, name: str) -> bool:
        return self._entry(folder, name) is not None

    def write(self, folder: str, name: str, data: bytes) -> str:
        if self.exists(folder, name):
            raise FileExistsError(f"{GARMIN_ROOT}/{folder}/{name} exists; files on the watch are never overwritten")
        with tempfile.TemporaryDirectory(prefix="wristpull-mtp-") as tmp:
            src = Path(tmp) / name
            src.write_bytes(data)
            try:
                self.mtp.send(src, f"{GARMIN_ROOT}/{folder}", name)
            except MtpError as exc:
                raise OSError(str(exc)) from exc
        return "mtp"

    def remove(self, folder: str, name: str) -> str:
        entry = self._entry(folder, name)
        if entry is None:
            raise FileNotFoundError(f"{GARMIN_ROOT}/{folder}/{name} is not on the watch")
        try:
            self.mtp.delete(entry.id)
        except MtpError as exc:
            raise OSError(str(exc)) from exc
        return "mtp"

    def eject(self) -> tuple[bool, str]:
        return False, "MTP: nothing to eject, the tools close the device after every call"


def open_watch(
    config: Config,
    path: Path | None = None,
    *,
    roots: Callable[[], list[Path]] | None = None,
    mounter: Callable[[], tuple[bool, str]] = try_mount_garmin,
    access: str | None = None,
    mtp_factory: MtpFactory | None = None,
    runner: Runner = run_command,
) -> tuple[WatchFiles | None, str | None]:
    """The watch as :class:`WatchFiles`, or ``(None, why)``. An explicit ``path`` is always a directory;
    otherwise ``access`` (default ``[garmin] access``) chooses between the mount and the MTP tools."""
    if path is None and (access or resolve_access(config)) == "mtp":
        mtp = (mtp_factory or MtpTools)()
        info, message = probe_mtp(mtp)
        return (MtpWatch(mtp, info) if info is not None else None), message
    root, message = locate_garmin(config, path, roots=roots, mounter=mounter)
    return (LocalWatch(root, runner=runner) if root is not None else None), message


def watch_present(config: Config, *, roots: Callable[[], list[Path]] | None = None, access: str | None = None) -> bool | None:
    """Cheap presence check for status lines: a mounted GARMIN folder, or ``None`` over MTP (probing costs seconds)."""
    from ..garmin_usb import candidate_roots, find_garmin_root

    if (access or resolve_access(config)) == "mtp":
        return None
    search = roots or (lambda: candidate_roots(extra=config.garmin_mount_hints))
    return find_garmin_root(search()) is not None
