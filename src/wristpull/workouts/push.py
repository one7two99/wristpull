"""Copy workouts as FIT files onto the connected watch, and check which ones it took over."""

from __future__ import annotations

import errno
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..archive import sha256_bytes
from ..config import Config
from ..fit.workout import build_schedule_fit, build_workout_fit, workout_created
from ..garmin_usb import Runner, run_command, try_mount_garmin
from ..mtp import MtpFactory
from ..timeutil import iso_utc
from .device import LocalWatch, WatchFiles, open_watch
from .model import Workout, base_name, slug
from .store import mark_folder_clashes, mark_verified, output_dir, pending_pushes, record_push, set_status
from .watch import WORKOUT_LIMIT_WARN, list_watch_workouts

Log = Callable[[str], None]
FOLDERS = ("NewFiles", "Workouts")
GVFS_ERRNOS = {errno.ENOTSUP, errno.EOPNOTSUPP, errno.EIO, errno.EINVAL}


def write_device_file(target: Path, data: bytes, *, runner: Runner = run_command) -> str:
    """Write ``data`` to ``target``; on a gvfs MTP mount plain writes fail with ENOTSUP, so fall back to ``gio copy``.

    Returns ``'write'`` or ``'gio'`` for the log.
    """
    try:
        target.write_bytes(data)
        return "write"
    except OSError as exc:
        if exc.errno not in GVFS_ERRNOS:
            raise
    with tempfile.NamedTemporaryFile(suffix=".fit", delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        code, out, err = runner(["gio", "copy", str(tmp_path), str(target)])
    finally:
        tmp_path.unlink(missing_ok=True)
    if code != 0:
        raise OSError(f"gio copy to {target} failed: {(err or out).strip() or code}")
    return "gio"


@dataclass
class PushResult:
    root: Path | None = None  # the GARMIN directory (over MTP the device path /GARMIN)
    target: Path | None = None
    dry_run: bool = False
    written: list[Path] = field(default_factory=list)
    invalid: list[tuple[str, list[str]]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)  # (file name, why) for files the device refused
    schedule: Path | None = None
    mount_message: str | None = None
    device: str | None = None  # how the watch was reached, for the log
    ejected: bool = False
    eject_message: str | None = None

    @property
    def ok(self) -> bool:
        return self.root is not None and not self.invalid and not self.errors


@dataclass
class VerifyResult:
    root: Path | None = None
    on_watch: list[str] = field(default_factory=list)  # workout names found under GARMIN/Workouts
    verified: list[str] = field(default_factory=list)  # pending pushes now confirmed
    missing: list[str] = field(default_factory=list)  # pending pushes not (yet) on the watch
    mount_message: str | None = None

    @property
    def near_limit(self) -> bool:
        return len(self.on_watch) >= WORKOUT_LIMIT_WARN

    @property
    def limit_warning(self) -> str | None:
        if not self.near_limit:
            return None
        return f"{len(self.on_watch)} workouts on the watch: the fenix 6 stores 20-25; above that it drops new files silently. Delete old ones under GARMIN/Workouts before pushing more."


def device_folder(root: Path, name: str) -> Path:
    """``GARMIN/<name>`` matching the existing folder's case (the fenix has NewFiles and Workouts)."""
    return LocalWatch(root).folder_path(name)


def push(
    conn: sqlite3.Connection,
    config: Config,
    workouts: list[Workout],
    *,
    path: Path | None = None,
    roots: Callable[[], list[Path]] | None = None,
    mounter: Callable[[], tuple[bool, str]] = try_mount_garmin,
    schedule: bool | None = None,
    folder: str | None = None,
    dry_run: bool = False,
    eject: bool | None = None,
    force: bool = False,
    log: Log = lambda _m: None,
    runner: Runner = run_command,
    access: str | None = None,
    mtp_factory: MtpFactory | None = None,
    watch: WatchFiles | None = None,
) -> PushResult:
    """Encode every valid workout and copy it to ``GARMIN/<folder>/`` (dry run: ``<data_dir>/workouts/out/``).

    Existing files on the watch are never overwritten or deleted; a changed workout gets a
    new file name carrying its content hash. ``eject`` (default: ``[workout] eject``, off) unmounts the
    MTP mount afterwards; the fenix 6X Pro keeps the files without it. ``force`` writes a file again
    even when one with the same content is already in the target folder (under a time-stamped name).
    The watch is reached through ``watch`` or :func:`open_watch` (mount or MTP tools, see ``access``).
    """
    result = PushResult(dry_run=dry_run)
    schedule = config.workout.schedule if schedule is None else schedule
    eject = config.workout.eject if eject is None else eject
    folder = folder or config.workout.folder
    valid: list[Workout] = []
    mark_folder_clashes(config, workouts)
    for workout in workouts:
        if workout.valid:
            valid.append(workout)
        else:
            result.invalid.append((workout.name or workout.path or "?", list(workout.errors)))
            log(f"  ! {workout.path or workout.name}: " + "; ".join(workout.errors))
    if not valid:
        return result
    serial: str | None = None
    if dry_run:
        out = output_dir(config)
        out.mkdir(parents=True, exist_ok=True)
        watch = LocalWatch(out.parent, runner=runner)  # <data_dir>/workouts/out as the "folder"
        folder = out.name
        result.root = out
    else:
        if watch is None:
            watch, result.mount_message = open_watch(config, path, roots=roots, mounter=mounter, access=access, mtp_factory=mtp_factory, runner=runner)
        if watch is None:
            return result
        result.root = watch.root
        result.device = watch.describe()
        serial = watch.unit_id()
    result.target = watch.folder_path(folder)
    created_at: dict[str, datetime] = {}
    for workout in valid:
        created_at[workout.name] = workout_created()
        data = build_workout_fit(workout, created=created_at[workout.name])
        digest = sha256_bytes(data)
        name = f"{workout.day.isoformat() if workout.day else 'undated'}-{slug(base_name(workout.name))}-{digest[:8]}.fit"
        if watch.exists(folder, name) and force:
            name = f"{name[:-4]}-{datetime.now(UTC).strftime('%H%M%S')}.fit"
        if watch.exists(folder, name):
            log(f"  = {name} already on the device (use force to write it again)")
        else:
            try:
                how = watch.write(folder, name, data)
            except OSError as exc:
                result.errors.append((name, str(exc)))
                log(f"  ! {name}: {exc}")
                continue
            log(f"  + {name} ({len(data)} bytes{', via gio copy' if how == 'gio' else ', over MTP' if how == 'mtp' else ''})")
        result.written.append(result.target / name)
        record_push(conn, workout, sha256=digest, time_created_utc=iso_utc(created_at[workout.name]), target=watch.locator(folder, name), watch_serial=serial, dry_run=dry_run)
        if not dry_run and workout.path:
            set_status(Path(workout.path), "pushed")
    if schedule and (result.written or not valid):
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        schedule_name = f"schedule-{stamp}.fit"
        try:
            watch.write(folder, schedule_name, build_schedule_fit(valid, workout_created_at=created_at))
            result.schedule = result.target / schedule_name
            log(f"  + {schedule_name} ({len(valid)} scheduled)")
        except OSError as exc:
            result.errors.append((schedule_name, str(exc)))
            log(f"  ! {schedule_name}: {exc}")
    if eject and not dry_run and result.root is not None and (result.written or result.schedule):
        result.ejected, result.eject_message = watch.eject()
        log(f"  {'⏏' if result.ejected else '!'} {result.eject_message}")
    return result


def workouts_on_watch(root: Path | WatchFiles) -> list[tuple[str, str]]:
    """``(wkt_name, time_created_utc)`` of every workout file directly in GARMIN/Workouts (active only)."""
    return [(item.workout.name if item.workout else item.name, item.time_created_utc) for item in list_watch_workouts(root) if item.error is None and item.location == "active"]


def verify(
    conn: sqlite3.Connection,
    config: Config,
    *,
    path: Path | None = None,
    roots: Callable[[], list[Path]] | None = None,
    mounter: Callable[[], tuple[bool, str]] = try_mount_garmin,
    log: Log = lambda _m: None,
    access: str | None = None,
    mtp_factory: MtpFactory | None = None,
    watch: WatchFiles | None = None,
) -> VerifyResult:
    """Read GARMIN/Workouts and confirm the pushes the watch has taken over (status ``verified``)."""
    result = VerifyResult()
    if watch is None:
        watch, result.mount_message = open_watch(config, path, roots=roots, mounter=mounter, access=access, mtp_factory=mtp_factory)
    if watch is None:
        return result
    result.root = watch.root
    present = workouts_on_watch(watch)
    result.on_watch = [name for name, _created in present]
    if result.limit_warning:
        log(f"  ! {result.limit_warning}")
    keys = set(present)
    for row in pending_pushes(conn):
        # name and time_created together: a same-named foreign workout must not count (the watch keeps both fields)
        hit = (row["name"], row["time_created_utc"]) in keys
        if hit:
            mark_verified(conn, int(row["id"]))
            if row.get("path") and Path(row["path"]).exists():
                set_status(Path(row["path"]), "verified")
            result.verified.append(row["name"])
            log(f"  ✓ {row['name']} is on the watch")
        else:
            result.missing.append(row["name"])
            log(f"  ? {row['name']} not found under GARMIN/Workouts yet")
    return result
