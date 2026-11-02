"""Workouts already on the watch: list them, back them up, remove them on request.

Only top-level files in ``GARMIN/Workouts/`` are touched; the sub-folders (``.Intervals``, ``Guided``,
``Schedule``) and every raw-data folder stay read-only. Every removal is preceded by a copy into
``<data_dir>/workouts/watch-backup/``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..archive import sha256_bytes
from ..config import Config
from ..fit.decode import decode_fit
from ..fit.workout import workout_from_fit
from ..garmin_usb import Runner, run_command, try_mount_garmin
from ..importers.garmin import locate_garmin
from ..timeutil import iso_utc
from .model import Workout
from .store import mark_removed, push_rows_for, workouts_dir

Log = Callable[[str], None]
# Garmin states 25 stored workouts for the fenix 6 series; users hit the wall at about 20. Above the limit
# the watch silently deletes every new file instead of importing it.
WORKOUT_LIMIT = 25
WORKOUT_LIMIT_WARN = 20


@dataclass
class WatchWorkout:
    path: Path
    name: str
    sport: str
    steps: int
    time_created_utc: str
    size: int
    mtime_utc: str
    sha256: str
    origin: str  # "wristpull" | "other"
    workout: Workout | None = None
    error: str | None = None

    @property
    def key(self) -> str:
        return self.path.name


@dataclass
class WatchListing:
    root: Path | None = None
    items: list[WatchWorkout] = field(default_factory=list)
    mount_message: str | None = None

    @property
    def near_limit(self) -> bool:
        return len(self.items) >= WORKOUT_LIMIT_WARN


@dataclass
class RemoveResult:
    removed: list[str] = field(default_factory=list)  # file names
    backups: list[Path] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    listing: WatchListing | None = None  # the watch re-read after removing


def workouts_folder(root: Path) -> Path:
    """``GARMIN/Workouts`` matching the existing folder's case."""
    try:
        for child in root.iterdir():
            if child.is_dir() and child.name.lower() == "workouts":
                return child
    except OSError:
        pass
    return root / "Workouts"


def backup_dir(config: Config) -> Path:
    return workouts_dir(config) / "watch-backup"


def read_watch_workout(path: Path, conn: sqlite3.Connection | None = None) -> WatchWorkout:
    data = path.read_bytes()
    st = path.stat()
    digest = sha256_bytes(data)
    item = WatchWorkout(path=path, name=path.stem, sport="?", steps=0, time_created_utc="", size=st.st_size, mtime_utc=iso_utc(datetime.fromtimestamp(st.st_mtime, UTC)), sha256=digest, origin="other")
    try:
        fit = decode_fit(data, check_crc=False)
        workout = workout_from_fit(fit)
    except Exception as exc:  # noqa: BLE001 - foreign files on the watch may be anything
        item.error = f"{type(exc).__name__}: {exc}"
        return item
    item.workout = workout
    item.name = workout.name or path.stem
    item.sport = workout.sport
    item.steps = len(workout.steps)
    item.time_created_utc = fit.time_created_iso or ""
    if conn is not None and push_rows_for(conn, sha256=digest, name=workout.name, time_created_utc=item.time_created_utc):
        item.origin = "wristpull"
    return item


def list_watch_workouts(root: Path, conn: sqlite3.Connection | None = None) -> list[WatchWorkout]:
    """Every ``*.fit`` directly in ``GARMIN/Workouts`` (sub-folders are ignored), sorted by name."""
    folder = workouts_folder(root)
    if not folder.is_dir():
        return []
    items: list[WatchWorkout] = []
    for path in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if path.is_file() and path.suffix.lower() == ".fit":
            try:
                items.append(read_watch_workout(path, conn))
            except OSError:
                continue
    items.sort(key=lambda w: w.name.lower())
    return items


def read_watch(
    conn: sqlite3.Connection,
    config: Config,
    *,
    path: Path | None = None,
    roots: Callable[[], list[Path]] | None = None,
    mounter: Callable[[], tuple[bool, str]] = try_mount_garmin,
) -> WatchListing:
    listing = WatchListing()
    root, listing.mount_message = locate_garmin(config, path, roots=roots, mounter=mounter)
    if root is None:
        return listing
    listing.root = root
    listing.items = list_watch_workouts(root, conn)
    return listing


def backup_watch_workout(config: Config, item: WatchWorkout) -> Path:
    """Copy the file into ``workouts/watch-backup/``; an identical earlier backup is reused."""
    folder = backup_dir(config)
    folder.mkdir(parents=True, exist_ok=True)
    data = item.path.read_bytes()
    digest = sha256_bytes(data)
    for existing in folder.glob(f"*-{item.path.name}"):
        try:
            if sha256_bytes(existing.read_bytes()) == digest:
                return existing
        except OSError:
            continue
    target = folder / f"{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{item.path.name}"
    target.write_bytes(data)
    return target


def remove_device_file(path: Path, *, runner: Runner = run_command) -> str:
    """Delete a file on the watch; gvfs MTP mounts may refuse ``unlink``, then ``gio remove`` does it."""
    try:
        path.unlink()
        return "unlink"
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            raise
    code, out, err = runner(["gio", "remove", str(path)])
    if code != 0:
        raise OSError(f"gio remove {path} failed: {(err or out).strip() or code}")
    return "gio"


def remove_watch_workout(conn: sqlite3.Connection, config: Config, item: WatchWorkout, *, runner: Runner = run_command) -> Path:
    """Back up, then delete one workout file from ``GARMIN/Workouts``; matching pushes become ``removed``.

    Returns the backup path. Refuses anything outside the top level of a ``Workouts`` folder.
    """
    if item.path.parent.name.lower() != "workouts" or item.path.suffix.lower() != ".fit":
        raise ValueError(f"refusing to remove {item.path}: only workout files directly in GARMIN/Workouts")
    backup = backup_watch_workout(config, item)
    remove_device_file(item.path, runner=runner)
    rows = push_rows_for(conn, sha256=item.sha256, name=item.workout.name if item.workout else None, time_created_utc=item.time_created_utc)
    mark_removed(conn, [int(r["id"]) for r in rows])
    return backup


def remove_from_watch(
    conn: sqlite3.Connection,
    config: Config,
    keys: list[str],
    *,
    path: Path | None = None,
    roots: Callable[[], list[Path]] | None = None,
    mounter: Callable[[], tuple[bool, str]] = try_mount_garmin,
    runner: Runner = run_command,
    log: Log = lambda _m: None,
) -> RemoveResult:
    """Remove the workouts whose file name or workout name is in ``keys``; re-reads the watch afterwards."""
    result = RemoveResult()
    listing = read_watch(conn, config, path=path, roots=roots, mounter=mounter)
    if listing.root is None:
        result.listing = listing
        return result
    wanted = {k.lower() for k in keys}
    matched: set[str] = set()
    for item in listing.items:
        hit = {item.path.name.lower(), item.path.stem.lower(), item.name.lower()} & wanted
        if not hit:
            continue
        matched |= hit
        try:
            backup = remove_watch_workout(conn, config, item, runner=runner)
        except (OSError, ValueError) as exc:
            result.errors.append((item.path.name, str(exc)))
            log(f"  ! {item.path.name}: {exc}")
            continue
        result.removed.append(item.path.name)
        result.backups.append(backup)
        log(f"  - {item.name} ({item.path.name}), backup {backup.name}")
    for key in sorted(wanted - matched):
        result.errors.append((key, "not found under GARMIN/Workouts"))
        log(f"  ? {key}: not found under GARMIN/Workouts")
    result.listing = read_watch(conn, config, path=listing.root)
    return result
