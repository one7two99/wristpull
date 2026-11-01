"""Pull Gadgetbridge data from the phone over ADB and import it.

Two things come from the phone:

* the exported ``Gadgetbridge.db`` (Zepp OS / Helio Strap samples, workouts)
* the Garmin FIT files Gadgetbridge downloaded over Bluetooth, stored under
  ``Android/data/nodomain.freeyourgadget.gadgetbridge/files/<MAC>/<TYPE>/<year>/``

Both are archived like USB files (SHA-256 de-duplicated, immutable) and parsed
into the same tables, with sources ``gadgetbridge_zepp`` and ``gadgetbridge_garmin``.
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..adb import Adb, AdbError, NoDeviceError
from ..archive import RawArchive, sha256_bytes
from ..config import Config
from ..daily import rebuild_daily_summary
from ..gadgetbridge import SOURCE_GARMIN, SOURCE_ZEPP, parse_gadgetbridge_db
from ..timeutil import zone
from .garmin import import_file, store_parsed

GB_PACKAGE = "nodomain.freeyourgadget.gadgetbridge"
GB_FILES_DIR = f"/sdcard/Android/data/{GB_PACKAGE}/files"
DB_NAME = "Gadgetbridge.db"
ACTION_SYNC = f"{GB_PACKAGE}.command.ACTIVITY_SYNC"
ACTION_EXPORT = f"{GB_PACKAGE}.command.TRIGGER_DATABASE_EXPORT"
SOURCE_DB = "gadgetbridge"  # raw_files.source for the database export itself
FIT_KINDS = {"monitor": "monitor", "activity": "activity", "sleep": "sleep", "metrics": "metrics", "hrvstatus": "hrv", "hrv": "hrv"}
Log = Callable[[str], None]


@dataclass
class PullStats:
    device: str | None = None
    db_pulled: bool = False
    db_new: bool = False
    db_devices: list[str] = field(default_factory=list)
    fit_found: int = 0
    fit_new: int = 0
    fit_skipped: int = 0
    errors: int = 0
    rows: dict[str, int] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)
    stopped: bool = False

    def add_rows(self, counts: dict[str, int]) -> None:
        for key, value in counts.items():
            self.rows[key] = self.rows.get(key, 0) + value


def pick_device(adb: Adb, serial: str | None) -> str:
    devices = adb.devices()
    ready = [d for d in devices if d.state == "device"]
    if serial:
        if any(d.serial == serial for d in ready):
            return serial
        states = ", ".join(f"{d.serial} ({d.state})" for d in devices) or "none"
        raise NoDeviceError(f"phone {serial} not connected; adb sees: {states}")
    if len(ready) == 1:
        return ready[0].serial
    if not ready:
        raise NoDeviceError("no authorized device connected")
    raise NoDeviceError("several devices connected, set [phone] serial in the config")


def trigger_export(adb: Adb, db_path: str, *, wait: float, sync: bool, log: Log, sleep=time.sleep, clock=time.monotonic, stop: Callable[[], bool] | None = None) -> bool:
    """Ask Gadgetbridge (Intent API) to sync and export; wait until the export file changes."""
    before = adb.stat_mtime(db_path)
    if sync:
        adb.broadcast(ACTION_SYNC)
    adb.broadcast(ACTION_EXPORT)
    deadline = clock() + wait
    while clock() < deadline:
        if stop is not None and stop():
            log("  stopped on request while waiting for the export")
            return False
        sleep(2)
        now = adb.stat_mtime(db_path)
        if now is not None and now != before:
            log(f"  export written at {datetime.fromtimestamp(now, UTC).strftime('%H:%M:%S')} UTC")
            return True
    log("  export did not change within the wait time; using the existing file (check the Intent API settings)")
    return False


def import_gadgetbridge_db(conn: sqlite3.Connection, config: Config, db_file: Path, *, origin: str | None = None, mtime: datetime | None = None, log: Log = lambda _m: None) -> tuple[bool, dict[str, int], list[str]]:
    """Archive one exported database and import its non-Garmin devices.

    Returns (is_new, inserted counts, device labels).
    """
    data = db_file.read_bytes()
    archive = RawArchive(config.raw_dir, conn)
    stored = archive.store(
        data,
        source=SOURCE_DB,
        kind="database",
        original_name=db_file.name,
        origin_path=origin,
        device_mtime=mtime,
        year=(mtime or datetime.now(UTC)).strftime("%Y"),
    )
    if not stored.is_new:
        return False, {}, []
    try:
        parsed, devices = parse_gadgetbridge_db(stored.archive_path, zone(config.timezone))
        counts = store_parsed(conn, parsed, stored.raw_file_id)
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        archive.mark_parsed(stored.raw_file_id, status="error", error=f"{type(exc).__name__}: {exc}")
        conn.commit()
        raise
    archive.mark_parsed(stored.raw_file_id, status="ok", fit_file_type="gadgetbridge_db")
    conn.commit()
    labels = [f"{d.name or d.type_name} [{d.mac}] -> {d.source or 'ignored'}" for d in devices]
    return True, counts, labels


def kind_from_path(path: str) -> str:
    parts = [p.lower() for p in path.split("/")]
    for part in parts:
        if part in FIT_KINDS:
            return FIT_KINDS[part]
    return "other"


def import_gadgetbridge_fit_dir(conn: sqlite3.Connection, config: Config, files_dir: Path, *, log: Log = lambda _m: None) -> tuple[int, int, int, int, dict[str, int]]:
    """Import every FIT file below a local copy of Gadgetbridge's files directory.

    Returns (found, new, skipped, errors, counts).
    """
    archive = RawArchive(config.raw_dir, conn)
    found = new = skipped = errors = 0
    counts: dict[str, int] = {}
    for path in sorted(files_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() != ".fit":
            continue
        found += 1
        status, inserted = import_file(
            conn,
            archive,
            path.read_bytes(),
            kind=kind_from_path(str(path.relative_to(files_dir))),
            original_name=path.name,
            origin_path=str(path),
            device_mtime=datetime.fromtimestamp(path.stat().st_mtime, UTC),
            tz_name=config.timezone,
            source=SOURCE_GARMIN,
        )
        if status == "new":
            new += 1
            for k, v in inserted.items():
                counts[k] = counts.get(k, 0) + v
            log(f"  + {path.relative_to(files_dir)}: " + (", ".join(f"{k}={v}" for k, v in inserted.items()) or "no rows"))
        elif status == "skipped":
            skipped += 1
        else:
            new += 1
            errors += 1
            log(f"  ! {path.relative_to(files_dir)}: decode error")
    return found, new, skipped, errors, counts


def pull_phone(
    conn: sqlite3.Connection,
    config: Config,
    *,
    adb: Adb | None = None,
    trigger: bool = True,
    sync: bool = True,
    wait: float = 60.0,
    dry_run: bool = False,
    log: Log = lambda _m: None,
    sleep=time.sleep,
    stop: Callable[[], bool] | None = None,
) -> PullStats:
    stats = PullStats()
    adb = adb or Adb(binary="adb")
    serial = pick_device(adb, config.phone_serial)
    adb.serial = serial
    stats.device = serial

    db_remote = f"{config.phone_export_dir.rstrip('/')}/{DB_NAME}"
    if trigger and not dry_run:
        try:
            trigger_export(adb, db_remote, wait=wait, sync=sync, log=log, sleep=sleep, stop=stop)
            if stop is not None and stop():
                stats.stopped = True
                return stats
        except AdbError as exc:
            stats.messages.append(f"export trigger failed: {exc}")
            log(f"  ! export trigger failed: {exc}")

    # which Garmin FIT files are new? hash on the device, pull only unknown ones
    archive = RawArchive(config.raw_dir, conn)
    remote_hashes = adb.file_hashes(GB_FILES_DIR)
    fit_remote = {p: h for p, h in remote_hashes.items() if p.lower().endswith(".fit")}
    stats.fit_found = len(fit_remote)
    to_pull = [p for p, h in fit_remote.items() if archive.lookup(h) is None]
    stats.fit_skipped = len(fit_remote) - len(to_pull)
    db_mtime = adb.stat_mtime(db_remote)

    if dry_run:
        stats.db_pulled = db_mtime is not None
        for p in to_pull:
            log(f"  + {p} would be pulled")
        stats.fit_new = len(to_pull)
        return stats

    with tempfile.TemporaryDirectory(prefix="wristpull-pull-") as tmp:
        tmp_dir = Path(tmp)
        if db_mtime is None:
            stats.messages.append(f"{db_remote} not found on the phone (configure Auto export in Gadgetbridge)")
            log(f"  ! {db_remote} not found on the phone")
        else:
            local_db = tmp_dir / DB_NAME
            adb.pull(db_remote, local_db)
            stats.db_pulled = True
            try:
                is_new, counts, labels = import_gadgetbridge_db(
                    conn, config, local_db, origin=db_remote, mtime=datetime.fromtimestamp(db_mtime, UTC), log=log
                )
            except Exception as exc:  # noqa: BLE001
                stats.errors += 1
                stats.messages.append(f"database import failed: {exc}")
                is_new, counts, labels = False, {}, []
            stats.db_new = is_new
            stats.db_devices = labels
            stats.add_rows(counts)
            log(f"  {DB_NAME}: " + ("imported, " + (", ".join(f"{k}={v}" for k, v in counts.items()) or "no new rows") if is_new else "unchanged since last pull"))

        if to_pull:
            fit_dir = tmp_dir / "files"
            for remote in to_pull:
                if stop is not None and stop():
                    stats.stopped = True
                    log("  stopped on request")
                    break
                rel = Path(remote[len(GB_FILES_DIR) :].lstrip("/"))
                local = fit_dir / rel
                local.parent.mkdir(parents=True, exist_ok=True)
                try:
                    adb.pull(remote, local)
                except AdbError as exc:
                    stats.errors += 1
                    stats.messages.append(f"pull failed for {remote}: {exc}")
            if fit_dir.exists():
                found, new, skipped, errors, counts = import_gadgetbridge_fit_dir(conn, config, fit_dir, log=log)
                stats.fit_new = new
                stats.errors += errors
                stats.add_rows(counts)

    if stats.rows:
        for source in (SOURCE_ZEPP, SOURCE_GARMIN):
            rebuild_daily_summary(conn, source=source)
        conn.commit()
    return stats


def verify_sha(data: bytes, expected: str) -> bool:
    return sha256_bytes(data) == expected
