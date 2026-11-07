"""Workouts already on the watch: list them, back them up, remove them on request.

Two folders are read: ``GARMIN/Workouts/`` (active, the watch lists them) and ``GARMIN/NewFiles/``
(pending: pushed, imported by the watch when the cable is pulled). Only top-level workout files in
these two folders are touched; the sub-folders (``.Intervals``, ``Guided``,
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
from ..mtp import MtpFactory
from ..timeutil import iso_utc
from .device import LocalWatch, WatchFiles, open_watch
from .model import Workout
from .store import mark_removed, push_rows_for, workouts_dir

Log = Callable[[str], None]
# Garmin states 25 stored workouts for the fenix 6 series; users hit the wall at about 20. Above the limit
# the watch silently deletes every new file instead of importing it.
WORKOUT_LIMIT = 25
WORKOUT_LIMIT_WARN = 20


@dataclass
class WatchWorkout:
    path: Path  # on a mount the file itself; over MTP the device path (/GARMIN/Workouts/x.fit)
    name: str
    sport: str
    steps: int
    time_created_utc: str
    size: int
    mtime_utc: str | None  # None over MTP
    sha256: str
    origin: str  # "wristpull" | "other"
    workout: Workout | None = None
    error: str | None = None
    location: str = "active"  # "active" (GARMIN/Workouts) | "pending" (GARMIN/NewFiles, active after unplugging)
    name_clash: bool = False  # a pending workout shares its name with another one
    locator: str = ""  # as recorded in workout_pushes.target
    data: bytes = field(default=b"", repr=False)  # the bytes read while listing (backup and removal reuse them)

    @property
    def key(self) -> str:
        return f"{self.location}/{self.path.name}"


@dataclass
class WatchListing:
    root: Path | None = None
    items: list[WatchWorkout] = field(default_factory=list)
    mount_message: str | None = None
    watch: WatchFiles | None = None  # how the watch was reached (reused for a cheap re-read after removing)
    device: str | None = None

    @property
    def active(self) -> list[WatchWorkout]:
        return [i for i in self.items if i.location == "active"]

    @property
    def pending(self) -> list[WatchWorkout]:
        return [i for i in self.items if i.location == "pending"]

    @property
    def near_limit(self) -> bool:
        """Counts pending files too: after unplugging they are on the watch as well."""
        return len(self.items) >= WORKOUT_LIMIT_WARN


@dataclass
class RemoveResult:
    removed: list[str] = field(default_factory=list)  # file names
    backups: list[Path] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    listing: WatchListing | None = None  # the watch re-read after removing


LOCATIONS = {"active": "Workouts", "pending": "NewFiles"}


def garmin_subfolder(root: Path, name: str) -> Path:
    """``GARMIN/<name>`` matching the existing folder's case."""
    return LocalWatch(root).folder_path(name)


def workouts_folder(root: Path) -> Path:
    return garmin_subfolder(root, "Workouts")


def backup_dir(config: Config) -> Path:
    return workouts_dir(config) / "watch-backup"


def read_watch_workout(path: Path, conn: sqlite3.Connection | None = None, *, location: str = "active") -> WatchWorkout:
    """A workout file on a mount (reads and parses it)."""
    data = path.read_bytes()
    st = path.stat()
    return watch_workout_from_bytes(path, data, conn, location=location, mtime_utc=iso_utc(datetime.fromtimestamp(st.st_mtime, UTC)), locator=str(path))


def watch_workout_from_bytes(path: Path, data: bytes, conn: sqlite3.Connection | None = None, *, location: str = "active", mtime_utc: str | None = None, locator: str = "") -> WatchWorkout:
    """Parse the bytes of a file on the watch; ``path`` only names it (``.name``, ``.parent.name``)."""
    digest = sha256_bytes(data)
    item = WatchWorkout(path=path, name=path.stem, sport="?", steps=0, time_created_utc="", size=len(data), mtime_utc=mtime_utc, sha256=digest, origin="other", location=location, locator=locator or str(path), data=data)
    try:
        fit = decode_fit(data, check_crc=False)
        if location == "pending" and fit.file_type != "workout":
            raise ValueError(f"not a workout file ({fit.file_type})")
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


def list_watch_workouts(root: Path | WatchFiles, conn: sqlite3.Connection | None = None, *, known: dict[str, WatchWorkout] | None = None) -> list[WatchWorkout]:
    """Workout files directly in ``GARMIN/Workouts`` (active) and ``GARMIN/NewFiles`` (pending).

    Sub-folders are ignored; in ``NewFiles`` only FIT files of type workout are listed (schedule files and
    anything else stay invisible). Active first, then pending, each by name. ``root`` is a mounted GARMIN
    directory or a :class:`WatchFiles`; ``known`` (key -> item of an earlier listing) spares re-reading files
    whose name and size are unchanged, which matters over MTP (one fetch per file).
    """
    watch: WatchFiles = root if not isinstance(root, Path) else LocalWatch(root)
    items: list[WatchWorkout] = []
    for location, name in LOCATIONS.items():
        folder_path = watch.folder_path(name)
        for entry in watch.list(name):
            path = folder_path / entry.name
            earlier = known.get(f"{location}/{entry.name}") if known else None
            if earlier is not None and earlier.size == entry.size:
                items.append(earlier)
                continue
            try:
                data = watch.read(name, entry.name)
            except OSError:
                continue
            item = watch_workout_from_bytes(path, data, conn, location=location, mtime_utc=entry.mtime_utc, locator=watch.locator(name, entry.name))
            if location == "pending" and item.workout is None:
                continue
            items.append(item)
    names: dict[str, int] = {}
    for item in items:
        names[item.name.lower()] = names.get(item.name.lower(), 0) + 1
    for item in items:
        item.name_clash = item.location == "pending" and names[item.name.lower()] > 1
    items.sort(key=lambda w: (w.location != "active", w.name.lower(), w.path.name))
    return items


def read_watch(
    conn: sqlite3.Connection,
    config: Config,
    *,
    path: Path | None = None,
    roots: Callable[[], list[Path]] | None = None,
    mounter: Callable[[], tuple[bool, str]] = try_mount_garmin,
    access: str | None = None,
    mtp_factory: MtpFactory | None = None,
    runner: Runner = run_command,
    watch: WatchFiles | None = None,
    known: dict[str, WatchWorkout] | None = None,
) -> WatchListing:
    """List the workouts on the watch (``watch`` reuses an open connection, else see :func:`open_watch`)."""
    listing = WatchListing()
    if watch is None:
        watch, listing.mount_message = open_watch(config, path, roots=roots, mounter=mounter, access=access, mtp_factory=mtp_factory, runner=runner)
    if watch is None:
        return listing
    listing.root = watch.root
    listing.watch = watch
    listing.device = watch.describe()
    listing.items = list_watch_workouts(watch, conn, known=known)
    return listing


def backup_watch_workout(config: Config, item: WatchWorkout) -> Path:
    """Copy the file into ``workouts/watch-backup/``; an identical earlier backup is reused."""
    folder = backup_dir(config)
    folder.mkdir(parents=True, exist_ok=True)
    data = item.data or item.path.read_bytes()
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


def remove_watch_workout(conn: sqlite3.Connection, config: Config, item: WatchWorkout, *, runner: Runner = run_command, watch: WatchFiles | None = None) -> Path:
    """Back up, then delete one workout file from ``GARMIN/Workouts``; matching pushes become ``removed``.

    Returns the backup path. Refuses anything outside the top level of a ``Workouts`` folder.
    """
    if item.path.parent.name.lower() not in ("workouts", "newfiles") or item.path.suffix.lower() != ".fit":
        raise ValueError(f"refusing to remove {item.path}: only workout files directly in GARMIN/Workouts or GARMIN/NewFiles")
    backup = backup_watch_workout(config, item)
    watch = watch or LocalWatch(item.path.parent.parent, runner=runner)
    watch.remove(item.path.parent.name, item.path.name)
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
    access: str | None = None,
    mtp_factory: MtpFactory | None = None,
    watch: WatchFiles | None = None,
) -> RemoveResult:
    """Remove the workouts whose file name or workout name is in ``keys``; re-reads the watch afterwards."""
    result = RemoveResult()
    listing = read_watch(conn, config, path=path, roots=roots, mounter=mounter, access=access, mtp_factory=mtp_factory, runner=runner, watch=watch)
    if listing.watch is None:
        result.listing = listing
        return result
    wanted = {k.lower() for k in keys}
    matched: set[str] = set()
    for item in listing.items:
        hit = {item.key.lower(), item.path.name.lower(), item.path.stem.lower(), item.name.lower()} & wanted
        if not hit:
            continue
        matched |= hit
        try:
            backup = remove_watch_workout(conn, config, item, runner=runner, watch=listing.watch)
        except (OSError, ValueError) as exc:
            result.errors.append((item.path.name, str(exc)))
            log(f"  ! {item.path.name}: {exc}")
            continue
        result.removed.append(item.key)
        result.backups.append(backup)
        log(f"  - {item.name} ({item.path.name}), backup {backup.name}")
    for key in sorted(wanted - matched):
        result.errors.append((key, "not found under GARMIN/Workouts or NewFiles"))
        log(f"  ? {key}: not found under GARMIN/Workouts or NewFiles")
    result.listing = read_watch(conn, config, watch=listing.watch, known={i.key: i for i in listing.items if i.key not in result.removed})
    return result
