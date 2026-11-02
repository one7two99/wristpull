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
from ..fit.decode import decode_fit
from ..fit.workout import build_schedule_fit, build_workout_fit, workout_created
from ..garmin_usb import Runner, read_device_info, run_command, try_mount_garmin
from ..importers.garmin import locate_garmin
from ..timeutil import iso_utc
from .model import Workout, slug
from .store import mark_verified, output_dir, pending_pushes, record_push, set_status

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
    root: Path | None = None
    target: Path | None = None
    dry_run: bool = False
    written: list[Path] = field(default_factory=list)
    invalid: list[tuple[str, list[str]]] = field(default_factory=list)
    schedule: Path | None = None
    mount_message: str | None = None

    @property
    def ok(self) -> bool:
        return self.root is not None and not self.invalid


@dataclass
class VerifyResult:
    root: Path | None = None
    on_watch: list[str] = field(default_factory=list)  # workout names found under GARMIN/Workouts
    verified: list[str] = field(default_factory=list)  # pending pushes now confirmed
    missing: list[str] = field(default_factory=list)  # pending pushes not (yet) on the watch
    mount_message: str | None = None


def device_folder(root: Path, name: str) -> Path:
    """``GARMIN/<name>`` matching the existing folder's case (the fenix has NewFiles and Workouts)."""
    try:
        for child in root.iterdir():
            if child.is_dir() and child.name.lower() == name.lower():
                return child
    except OSError:
        pass
    return root / name


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
    log: Log = lambda _m: None,
    runner: Runner = run_command,
) -> PushResult:
    """Encode every valid workout and copy it to ``GARMIN/<folder>/`` (dry run: ``<data_dir>/workouts/out/``).

    Existing files on the watch are never overwritten or deleted; a changed workout gets a
    new file name carrying its content hash.
    """
    result = PushResult(dry_run=dry_run)
    schedule = config.workout.schedule if schedule is None else schedule
    folder = folder or config.workout.folder
    valid: list[Workout] = []
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
        result.root = result.target = output_dir(config)
        result.target.mkdir(parents=True, exist_ok=True)
    else:
        root, result.mount_message = locate_garmin(config, path, roots=roots, mounter=mounter)
        if root is None:
            return result
        result.root = root
        result.target = device_folder(root, folder)
        if not result.target.is_dir():
            result.target.mkdir(parents=True, exist_ok=True)
        serial = read_device_info(root).get("unit_id")
    for workout in valid:
        data = build_workout_fit(workout)
        digest = sha256_bytes(data)
        name = f"{workout.day.isoformat() if workout.day else 'undated'}-{slug(workout.name)}-{digest[:8]}.fit"
        target = result.target / name
        if target.exists():
            log(f"  = {name} already on the device")
        else:
            how = write_device_file(target, data, runner=runner)
            log(f"  + {name} ({len(data)} bytes{', via gio copy' if how == 'gio' else ''})")
        result.written.append(target)
        record_push(conn, workout, sha256=digest, time_created_utc=iso_utc(workout_created(workout)), target=str(target), watch_serial=serial, dry_run=dry_run)
        if not dry_run and workout.path:
            set_status(Path(workout.path), "pushed")
    if schedule:
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        result.schedule = result.target / f"schedule-{stamp}.fit"
        write_device_file(result.schedule, build_schedule_fit(valid), runner=runner)
        log(f"  + {result.schedule.name} ({len(valid)} scheduled)")
    return result


def workouts_on_watch(root: Path) -> list[tuple[str, str]]:
    """``(wkt_name, time_created_utc)`` of every workout file under GARMIN/Workouts."""
    found: list[tuple[str, str]] = []
    folder = device_folder(root, "Workouts")
    if not folder.is_dir():
        return found
    for file in sorted(folder.rglob("*")):
        if not file.is_file() or file.suffix.lower() != ".fit":
            continue
        try:
            fit = decode_fit(file.read_bytes(), check_crc=False)
        except Exception:  # noqa: BLE001 - foreign files on the watch may be anything
            continue
        for msg in fit.of("workout"):
            found.append((str(msg.get("wkt_name") or ""), fit.time_created_iso or ""))
    return found


def verify(
    conn: sqlite3.Connection,
    config: Config,
    *,
    path: Path | None = None,
    roots: Callable[[], list[Path]] | None = None,
    mounter: Callable[[], tuple[bool, str]] = try_mount_garmin,
    log: Log = lambda _m: None,
) -> VerifyResult:
    """Read GARMIN/Workouts and confirm the pushes the watch has taken over (status ``verified``)."""
    result = VerifyResult()
    root, result.mount_message = locate_garmin(config, path, roots=roots, mounter=mounter)
    if root is None:
        return result
    result.root = root
    present = workouts_on_watch(root)
    result.on_watch = [name for name, _created in present]
    keys = set(present)
    names = {name for name, _created in present}
    for row in pending_pushes(conn):
        hit = (row["name"], row["time_created_utc"]) in keys or row["name"] in names
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
