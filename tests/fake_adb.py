"""An Adb stand-in serving a local directory tree as the phone's file system."""

from __future__ import annotations

import hashlib
import shlex
from pathlib import Path

from wristpull.adb import AdbError, Device, NoDeviceError


class FakeAdb:
    """``root`` mirrors the phone: <root>/sdcard/... . Files are hashed and pulled from there."""

    def __init__(self, root: Path, *, devices: list[Device] | None = None) -> None:
        self.root = Path(root)
        self.serial: str | None = None
        self._devices = devices if devices is not None else [Device("FAKE0001", "device")]
        self.calls: list[str] = []
        self.broadcasts: list[str] = []
        self.pulled: list[tuple[str, str]] = []
        self.export_on_trigger = False  # when True, a broadcast of the export action touches the DB mtime
        self.fail_pull: set[str] = set()
        self.connected: list[str] = []

    def _local(self, remote: str) -> Path:
        return self.root / remote.lstrip("/")

    def devices(self) -> list[Device]:
        return list(self._devices)

    def connect(self, address: str, *, timeout: float = 30.0) -> str:
        self.connected.append(address)
        if not any(d.serial == address for d in self._devices):
            raise NoDeviceError(f"failed to connect to {address}")
        return f"connected to {address}"

    def run(self, *args: str, timeout: float = 60.0, check: bool = True) -> str:
        self.calls.append(" ".join(args))
        return ""

    def shell(self, command: str, *, timeout: float = 60.0, check: bool = True) -> str:
        self.calls.append(command)
        return ""

    def broadcast(self, action: str, *, timeout: float = 30.0) -> str:
        self.broadcasts.append(action)
        if self.export_on_trigger and action.endswith("TRIGGER_DATABASE_EXPORT"):
            for db in self.root.rglob("Gadgetbridge.db"):
                stat = db.stat()
                import os

                os.utime(db, (stat.st_atime, stat.st_mtime + 60))
        return f"Broadcasting: Intent {{ act={action} }}\nBroadcast completed: result=0\n"

    def file_hashes(self, directory: str) -> dict[str, str]:
        self.calls.append(f"sha256sum {shlex.quote(directory)}")
        base = self._local(directory)
        out: dict[str, str] = {}
        if base.is_dir():
            for path in sorted(p for p in base.rglob("*") if p.is_file()):
                out[f"{directory.rstrip('/')}/{path.relative_to(base).as_posix()}"] = hashlib.sha256(path.read_bytes()).hexdigest()
        return out

    def stat_mtime(self, path: str) -> int | None:
        local = self._local(path)
        return int(local.stat().st_mtime) if local.exists() else None

    def pull(self, remote: str, local: Path, *, timeout: float = 900.0) -> str:
        self.pulled.append((remote, str(local)))
        if remote in self.fail_pull:
            raise AdbError(f"adb: error: remote object '{remote}' does not exist")
        src = self._local(remote)
        if not src.exists():
            raise NoDeviceError("device not found") if not self._devices else AdbError(f"remote object '{remote}' does not exist")
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        Path(local).write_bytes(src.read_bytes())
        return "1 file pulled\n"
