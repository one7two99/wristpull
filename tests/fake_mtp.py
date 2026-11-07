"""An Mtp stand-in serving a local directory tree as the watch's storage (like FakeAdb for the phone).

``root`` holds ``GARMIN/...`` exactly like the ``garmin_tree`` fixture's parent. Ids are assigned in
sorted walk order and never reused, so they stay stable across send and delete. ``send`` refuses an
existing name with an AssertionError: the real device would silently overwrite, callers must check.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from wristpull.mtp import MtpBusyError, MtpDeviceInfo, MtpError, MtpFile, NoMtpDeviceError, folder_id

FAKE_INFO = MtpDeviceInfo(vendor="Garmin", model="fenix 6 Pro", serial="0000deadbeef", version="2802", storages=("Primary",))


class FakeMtp:
    def __init__(self, root: Path, *, present: bool = True, info: MtpDeviceInfo = FAKE_INFO, busy: bool = False) -> None:
        self.root = Path(root)
        self.present = present
        self.busy = busy
        self.info = info
        self.ids: dict[str, int] = {}  # device path -> id
        self.calls: list[str] = []
        self.fetched: list[tuple[int, str]] = []
        self.sent: list[tuple[str, str]] = []
        self.deleted: list[int] = []
        self.fail_get: set[str] = set()  # device paths whose get() fails

    # -- helpers ----------------------------------------------------------------------------

    def _check(self, call: str) -> None:
        self.calls.append(call)
        if self.busy:
            raise MtpBusyError("busy")
        if not self.present:
            raise NoMtpDeviceError("no MTP device found")

    def _device_path(self, local: Path) -> str:
        return "/" + local.relative_to(self.root).as_posix()

    def _local(self, device_path: str) -> Path:
        return self.root / device_path.strip("/")

    def _id_of(self, device_path: str) -> int:
        if device_path not in self.ids:
            self.ids[device_path] = 16777216 + len(self.ids)
        return self.ids[device_path]

    def _walk(self) -> tuple[dict[int, str], list[MtpFile]]:
        folders: dict[int, str] = {}
        files: list[MtpFile] = []
        for local in sorted(p for p in self.root.rglob("*")):
            path = self._device_path(local)
            if local.is_dir():
                folders[self._id_of(path)] = path
            elif local.is_file():
                parent = self._device_path(local.parent) if local.parent != self.root else ""
                files.append(MtpFile(id=self._id_of(path), name=local.name, size=local.stat().st_size, parent_id=self._id_of(parent) if parent else 0, path=path))
        return folders, files

    def path_of(self, file_id: int) -> str | None:
        return next((path for path, fid in self.ids.items() if fid == file_id), None)

    # -- Mtp --------------------------------------------------------------------------------

    def device_info(self) -> MtpDeviceInfo:
        self._check("detect")
        return self.info

    def folders(self) -> dict[int, str]:
        self._check("folders")
        return self._walk()[0]

    def list(self, folder_path: str) -> list[MtpFile]:
        self._check(f"list {folder_path}")
        folders, files = self._walk()
        hit = folder_id(folders, folder_path)
        if hit is None:
            return []
        return sorted((f for f in files if f.parent_id == hit[0]), key=lambda f: f.name.lower())

    def get(self, file_id: int, dest: Path) -> None:
        self._check(f"get {file_id}")
        path = self.path_of(file_id)
        self.fetched.append((file_id, path or "?"))
        if path is None or path in self.fail_get or not self._local(path).is_file():
            raise MtpError(f"mtp-getfile {file_id}: Error getting file from MTP device.")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._local(path), dest)

    def send(self, src: Path, folder_path: str, name: str) -> int:
        self._check(f"send {folder_path}/{name}")
        hit = folder_id(self._walk()[0], folder_path)
        if hit is None:
            raise MtpError(f"no folder {folder_path} on the device")
        target = self._local(hit[1]) / name
        assert not target.exists(), f"FakeMtp: {name} already exists in {hit[1]}; callers must check first"
        shutil.copyfile(src, target)
        self.sent.append((hit[1], name))
        return self._id_of(self._device_path(target))

    def delete(self, file_id: int) -> None:
        self._check(f"delete {file_id}")
        path = self.path_of(file_id)
        if path is None or not self._local(path).is_file():
            raise MtpError(f"mtp-delfile {file_id}: Failed to delete file:{file_id}")
        self._local(path).unlink()
        self.deleted.append(file_id)

    def invalidate(self) -> None:
        self.calls.append("invalidate")


def fake_mtp_runner(fake: FakeMtp):
    """A ``Runner`` that answers like the mtp-tools binaries, backed by ``fake`` (same ids, same tree).

    Lets CLI tests drive the real ``MtpTools`` parsers end to end: patch ``wristpull.mtp.run_command``.
    """

    def runner(argv: list[str]) -> tuple[int, str, str]:
        tool, args = argv[0], argv[1:]
        try:
            if tool == "mtp-detect":
                info = fake.device_info()
                lines = ["libmtp version: 1.1.22", "", "Device info:", f"   Manufacturer: {info.vendor}", f"   Model: {info.model}", f"   Device version: {info.version}", f"   Serial number: {info.serial}"]
                lines += [f"   StorageDescription: {s}" for s in info.storages]
                return 0, "\n".join(lines) + "\n", ""
            if tool == "mtp-folders":
                folders = fake.folders()
                lines = ["Attempting to connect device(s)", "Friendly name: fake", "Storage: Primary"]
                for fid, path in sorted(folders.items(), key=lambda kv: kv[1].lower()):
                    parts = path.strip("/").split("/")
                    lines.append(f"{fid}\t{'  ' * (len(parts) - 1)}{parts[-1]}")
                return 0, "\n".join(lines) + "\nOK.\n", ""
            if tool == "mtp-files":
                _folders, files = fake._walk()
                fake._check("files")
                lines = ["libmtp version: 1.1.22", "", "Listing File Information on Device with name: fake"]
                for f in files:
                    lines += [f"File ID: {f.id}", f"   Filename: {f.name}", f"   File size {f.size} (0x{f.size:016X}) bytes", f"   Parent ID: {f.parent_id}", "   Storage ID: 0x00020001", "   Filetype: Undefined filetype"]
                return 0, "\n".join(lines) + "\nOK.\n", ""
            if tool == "mtp-getfile":
                try:
                    fake.get(int(args[0]), Path(args[1]))
                except MtpError:
                    return 1, "\nError getting file from MTP device.\n", ""
                return 0, f"Getting file/track {args[0]} to local file {args[1]}\nProgress: 1 of 1 (100%)\n", ""
            if tool == "mtp-sendfile":
                src, folder = Path(args[0]), args[1]
                try:
                    new_id = fake.send(src, folder, src.name)
                except MtpError:
                    return 0, f"Sending {src} to {folder}\nParent folder could not be found, skipping\n", ""
                return 0, f"Sending {src} to {folder}\ntype: fit, 44\nSending file...\nNew file ID: {new_id}\n", ""
            if tool == "mtp-delfile":
                fid = int(args[1])
                try:
                    fake.delete(fid)
                except MtpError:
                    return 1, f"Deleting {fid}\nFailed to delete file:{fid}\n", "Error 2: PTP Layer error 2002: LIBMTP_Delete_Object(): could not delete object.\n"
                return 0, f"Deleting {fid}\n", ""
        except MtpBusyError:
            return 0, "Attempting to connect device(s)\nOK.\n", "libusb_claim_interface() reports device is busy, likely in use by GVFS or KDE MTP device handling already\nLIBMTP PANIC: Unable to initialize device\nUnable to open raw device 0\n"
        except NoMtpDeviceError:
            return 0, "libmtp version: 1.1.22\n\nListing raw device(s)\n   No raw devices found.\n", ""
        return 127, "", f"{tool}: not found"

    return runner
