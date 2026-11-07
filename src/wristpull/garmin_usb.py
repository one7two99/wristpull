"""Locate a USB-connected Garmin watch and enumerate its FIT files.

A fenix 6 shows up on Linux as an MTP device (mounted by gvfs under
/run/user/<uid>/gvfs/mtp:host=.../<storage>/GARMIN), older devices as USB mass
storage (/run/media/<user>/GARMIN or /media/...). Both layouts are searched.
"""

from __future__ import annotations

import getpass
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

FOLDER_KINDS = {
    "activity": "activity",
    "monitor": "monitor",
    "sleep": "sleep",
    "metrics": "metrics",
    "hrv": "hrv",
    "hrvstatus": "hrv",  # folder name on the fenix 6X Pro
}


@dataclass(frozen=True)
class DeviceFile:
    path: Path  # where the bytes can be read (the mount, or a staging copy)
    kind: str
    folder: str
    name: str
    size: int
    mtime: datetime | None  # None when the transport does not report one (MTP tools)
    origin: str | None = None  # recorded as raw_files.origin_path; None: str(path)


def current_user() -> str:
    """The login name, or the numeric uid when there is no passwd entry (the container runs as the host uid)."""
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return str(os.getuid())


def candidate_roots(
    *,
    user: str | None = None,
    uid: int | None = None,
    extra: list[Path] | None = None,
) -> list[Path]:
    """Directories that might contain a GARMIN folder (existing ones only)."""
    user = user or current_user()
    uid = os.getuid() if uid is None else uid
    patterns: list[tuple[Path, str]] = [
        (Path(f"/run/user/{uid}/gvfs"), "mtp*/*"),
        (Path(f"/run/user/{uid}/gvfs"), "mtp*"),
        (Path(f"/run/media/{user}"), "*"),
        (Path(f"/media/{user}"), "*"),
        (Path("/media"), "*"),
        (Path("/mnt"), "*"),
    ]
    found: list[Path] = list(extra or [])
    for base, pattern in patterns:
        if not base.is_dir():
            continue
        try:
            found.extend(sorted(p for p in base.glob(pattern) if p.is_dir()))
        except OSError:
            continue
    return found


def garmin_dir(candidate: Path) -> Path | None:
    """Return <candidate>/GARMIN (any case) or candidate itself if it is a GARMIN dir."""
    try:
        if candidate.name.upper() == "GARMIN" and candidate.is_dir():
            return candidate
        for child in candidate.iterdir():
            if child.name.upper() == "GARMIN" and child.is_dir():
                return child
    except OSError:
        return None
    return None


def find_garmin_root(candidates: list[Path]) -> Path | None:
    for cand in candidates:
        hit = garmin_dir(cand)
        if hit is not None:
            return hit
    return None


def list_fit_files(garmin_root: Path, folders: list[str]) -> list[DeviceFile]:
    """FIT files in the given sub-folders of GARMIN/, tolerant of missing folders."""
    wanted = {f.lower(): f for f in folders}
    files: list[DeviceFile] = []
    try:
        children = list(garmin_root.iterdir())
    except OSError:
        return files
    for child in children:
        key = child.name.lower()
        if key not in wanted or not child.is_dir():
            continue
        kind = FOLDER_KINDS.get(key, "other")
        for path in sorted(child.rglob("*")):
            if not path.is_file() or path.suffix.lower() != ".fit":
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            files.append(
                DeviceFile(
                    path=path,
                    kind=kind,
                    folder=child.name,
                    name=path.name,
                    size=st.st_size,
                    mtime=datetime.fromtimestamp(st.st_mtime, UTC),
                )
            )
    return files


def read_device_info(garmin_root: Path) -> dict[str, str]:
    """Model and unit id from GARMIN/GarminDevice.xml, if present."""
    xml_path = next((p for p in garmin_root.iterdir() if p.name.lower() == "garmindevice.xml"), None) if garmin_root.is_dir() else None
    if xml_path is None:
        return {}
    try:
        return parse_device_info(xml_path.read_bytes())
    except OSError:
        return {}


def parse_device_info(xml: bytes) -> dict[str, str]:
    """``model``, ``unit_id`` and ``part_number`` from the bytes of GarminDevice.xml (empty when unparsable)."""
    info: dict[str, str] = {}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return info
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag == "Description" and el.text and "model" not in info:
            info["model"] = el.text.strip()
        elif tag == "Id" and el.text and "unit_id" not in info:
            info["unit_id"] = el.text.strip()
        elif tag == "PartNumber" and el.text and "part_number" not in info:
            info["part_number"] = el.text.strip()
    return info


# --- MTP auto-mount via gio ---------------------------------------------------

GARMIN_USB_VENDOR = "091e"
Runner = Callable[[list[str]], tuple[int, str, str]]


def run_command(argv: list[str], *, timeout: float = 60) -> tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr); a missing binary counts as failure (127)."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def gio_garmin_mtp_urls(runner: Runner = run_command) -> list[str]:
    """MTP activation URLs of unmounted Garmin devices as reported by ``gio mount -li``."""
    code, out, _err = runner(["gio", "mount", "-li"])
    if code != 0:
        return []
    urls: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("activation_root=mtp://"):
            url = line.split("=", 1)[1]
            if url.startswith(f"mtp://{GARMIN_USB_VENDOR}_"):
                urls.append(url)
    return urls


def kde_holds_mtp(runner: Runner = run_command) -> bool:
    """True when KDE's kiod6 (KIO MTP worker) is running; it keeps the device open."""
    code, _out, _err = runner(["pgrep", "-x", "kiod6"])
    return code == 0


KDE_HINT = (
    "The KDE service kiod6 is holding the MTP device, so gvfs cannot open it. Release it and mount:\n"
    "    kquitapp6 kiod6 && gio mount {url}\n"
    "then run the import again (or pass --path to the GARMIN folder)."
)


def release_kde_mtp(runner: Runner = run_command) -> tuple[bool, str]:
    """Quit KDE's kiod6 so gvfs can open the MTP device (KDE restarts it on demand)."""
    code, out, err = runner(["kquitapp6", "kiod6"])
    if code == 0:
        return True, "released the device from KDE (kquitapp6 kiod6)"
    return False, f"kquitapp6 kiod6 failed: {(err or out).strip() or code}"


def try_mount_garmin(runner: Runner = run_command, *, release_kde: bool = True, sleep: Callable[[float], None] = time.sleep) -> tuple[bool, str]:
    """Attempt ``gio mount`` for every unmounted Garmin MTP device.

    When KDE's kiod6 holds the device ("Unable to open MTP device"), it is released with
    ``kquitapp6 kiod6`` and the mount is tried once more (``release_kde``). Returns
    (mounted_any, message); a failure message carries the manual workaround.
    """
    urls = gio_garmin_mtp_urls(runner)
    if not urls:
        return False, "no Garmin MTP device reported by gio"
    messages: list[str] = []
    for url in urls:
        code, out, err = runner(["gio", "mount", url])
        if code == 0:
            return True, f"mounted {url}"
        text = (err or out).strip()
        if "Unable to open MTP device" in text and kde_holds_mtp(runner):
            if release_kde:
                released, note = release_kde_mtp(runner)
                if released:
                    sleep(1.5)
                    code, out, err = runner(["gio", "mount", url])
                    if code == 0:
                        return True, f"{note}; mounted {url}"
                    text = (err or out).strip()
                else:
                    text += f"\n{note}"
            text += "\n" + KDE_HINT.format(url=url)
        messages.append(f"gio mount {url} failed: {text}")
    return False, "\n".join(messages)


def mount_status(roots: list[Path], runner: Runner = run_command) -> str:
    """``mounted`` (a GARMIN folder is reachable), ``attached`` (gio sees an unmounted Garmin) or ``absent``."""
    if find_garmin_root(roots) is not None:
        return "mounted"
    return "attached" if gio_garmin_mtp_urls(runner) else "absent"


def remount_garmin(roots: list[Path] | None = None, runner: Runner = run_command, *, sleep: Callable[[float], None] = time.sleep) -> tuple[bool, str]:
    """Unmount any gvfs mount of the watch, then mount it again (releasing KDE's hold if needed).

    Fixes a stale mount after the cable was pulled and plugged in again.
    """
    notes: list[str] = []
    for root in roots or []:
        hit = garmin_dir(root)
        url = mtp_url_of(hit) if hit else None
        if url:
            code, out, err = runner(["gio", "mount", "-u", url])
            notes.append(f"unmounted {url}" if code == 0 else f"gio mount -u {url}: {(err or out).strip()}")
    mounted, message = try_mount_garmin(runner, sleep=sleep)
    return mounted, "; ".join(notes + [message])


GVFS_MTP = re.compile(r"/gvfs/mtp:host=([^/]+)/")


def mtp_url_of(path: Path) -> str | None:
    """``mtp://<id>/`` for a path below a gvfs MTP mount, else None."""
    m = GVFS_MTP.search(str(path))
    return f"mtp://{m.group(1)}/" if m else None


def eject_garmin(root: Path, runner: Runner = run_command) -> tuple[bool, str]:
    """Unmount (eject) the gvfs MTP mount that holds ``root`` so the watch commits written files.

    MTP devices may discard objects of a session that ends by unplugging; every Garmin guide ends
    with "eject the device". Returns (ejected, message); a non-MTP path is left alone.
    """
    url = mtp_url_of(root)
    if url is None:
        return False, "not an MTP mount, nothing to eject"
    code, out, err = runner(["gio", "mount", "-u", url])
    if code == 0:
        return True, f"ejected {url}"
    return False, f"gio mount -u {url} failed: {(err or out).strip()}"
