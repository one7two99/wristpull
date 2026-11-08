"""The watch as a directory: the ``GARMIN`` folder layout and its FIT files.

The watch itself is read over USB with the MTP tools (:mod:`wristpull.mtp`); this module serves the
directory form of the same tree, a copy made with a file manager or a mass-storage mount passed with
``--path``, and the file-kind rules both paths share.
"""

from __future__ import annotations

import subprocess
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


# --- running external tools ----------------------------------------------------

Runner = Callable[[list[str]], tuple[int, str, str]]


def run_command(argv: list[str], *, timeout: float = 60) -> tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr); a missing binary counts as failure (127)."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr
