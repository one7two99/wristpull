"""Import FIT files from a USB-connected Garmin watch (or any directory tree).

Three ways to the files: an explicit directory (``path``), the gvfs/USB mount found by
:func:`locate_garmin`, or the MTP tools (:mod:`wristpull.mtp`) which download new files into a
staging directory first. ``[garmin] access`` picks between the last two.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..archive import RawArchive, sha256_bytes
from ..config import Config
from ..daily import rebuild_daily_summary
from ..db import insert_row, insert_rows
from ..fit.decode import decode_fit
from ..fit.garmin import parse_fit
from ..rows import SAMPLE_TABLES, ParsedRows
from ..garmin_usb import FOLDER_KINDS, DeviceFile, candidate_roots, find_garmin_root, garmin_dir, list_fit_files, try_mount_garmin
from ..mtp import GARMIN_ROOT, Mtp, MtpError, MtpFactory, MtpTools, NoMtpDeviceError, folder_id, probe_mtp, resolve_access
from ..timeutil import zone

SOURCE = "garmin_usb"
MTP_ROOT_LABEL = "mtp://GARMIN"
Log = Callable[[str], None]
# raw_files.kind from the decoded FIT file type, for files that come without a GARMIN/<folder> context
# (the Garmin Connect export). Numeric types verified on a real export with `wristpull inspect`:
# 49 carries sleep_level messages, 68 hrv_value, 44 the metrics messages of the Metrics folder.
FIT_TYPE_KINDS = {"monitoring_b": "monitor", "activity": "activity", "49": "sleep", "68": "hrv", "44": "metrics"}


def kind_for_fit_type(file_type: str | None) -> str:
    return FIT_TYPE_KINDS.get(file_type or "", "other")


@dataclass
class ImportStats:
    root: Path | str | None = None  # the GARMIN directory, or "mtp://GARMIN" over MTP
    found: int = 0
    new: int = 0
    skipped: int = 0
    fetched: int = 0  # files downloaded over MTP (0 on a mount)
    discarded: int = 0  # JSON records dropped as placeholders/junk (Connect export)
    parsed_ok: int = 0
    errors: int = 0
    rows: dict[str, int] = field(default_factory=dict)
    error_files: list[tuple[str, str]] = field(default_factory=list)
    mount_message: str | None = None
    stopped: bool = False  # the caller asked to stop before all files were handled

    def add_rows(self, counts: dict[str, int]) -> None:
        for key, value in counts.items():
            self.rows[key] = self.rows.get(key, 0) + value


def store_parsed(conn: sqlite3.Connection, parsed: ParsedRows, raw_file_id: int) -> dict[str, int]:
    """Write parsed rows into the database; returns inserted counts per table."""
    counts: dict[str, int] = {}

    for act in parsed.activities:
        row = {**act.row, "raw_file_id": raw_file_id}
        existing = conn.execute(
            "SELECT id FROM activities WHERE source = ? AND start_utc = ? AND sport IS ?",
            (row["source"], row["start_utc"], row["sport"]),
        ).fetchone()
        if existing is not None:
            continue
        activity_id = insert_row(conn, "activities", row)
        counts["activities"] = counts.get("activities", 0) + 1
        counts["activity_records"] = counts.get("activity_records", 0) + insert_rows(
            conn, "activity_records", ({**r, "activity_id": activity_id} for r in act.records)
        )
        counts["activity_laps"] = counts.get("activity_laps", 0) + insert_rows(
            conn, "activity_laps", ({**lap, "activity_id": activity_id} for lap in act.laps)
        )

    for night in parsed.sleep:
        row = {**night.row, "raw_file_id": raw_file_id}
        existing = conn.execute(
            "SELECT id FROM sleep WHERE source = ? AND start_utc = ?", (row["source"], row["start_utc"])
        ).fetchone()
        if existing is not None:
            continue
        sleep_id = insert_row(conn, "sleep", row)
        counts["sleep"] = counts.get("sleep", 0) + 1
        counts["sleep_stages"] = counts.get("sleep_stages", 0) + insert_rows(
            conn, "sleep_stages", ({**s, "sleep_id": sleep_id} for s in night.stages)
        )

    for table in SAMPLE_TABLES:
        rows = getattr(parsed, table)
        if rows:
            counts[table] = insert_rows(
                conn,
                table,
                ({**r, "raw_file_id": raw_file_id} for r in rows),
                replace=table in parsed.replace_tables,
            )
    return {k: v for k, v in counts.items() if v}


def import_file(
    conn: sqlite3.Connection,
    archive: RawArchive,
    data: bytes,
    *,
    kind: str | None,
    original_name: str,
    origin_path: str | None,
    device_mtime,
    tz_name: str,
    source: str = SOURCE,
) -> tuple[str, dict[str, int]]:
    """Archive + parse one FIT file. Returns ('new'|'skipped'|'error', inserted counts).

    ``kind`` is the watch folder's kind; ``None`` derives it from the decoded FIT type
    (:func:`kind_for_fit_type`), which is what the Connect export needs.
    """
    digest = sha256_bytes(data)
    if archive.lookup(digest) is not None:
        return "skipped", {}

    fit = None
    error: str | None = None
    try:
        fit = decode_fit(data)
    except Exception as exc:  # noqa: BLE001 - any decode failure must not stop the import
        error = f"{type(exc).__name__}: {exc}"
    if kind is None:
        kind = kind_for_fit_type(fit.file_type if fit is not None else None)

    year = fit.time_created.strftime("%Y") if fit is not None and fit.time_created else None
    stored = archive.store(
        data,
        source=source,
        kind=kind,
        original_name=original_name,
        origin_path=origin_path,
        device_mtime=device_mtime,
        year=year,
    )
    if fit is None:
        archive.mark_parsed(stored.raw_file_id, status="error", error=error)
        conn.commit()
        return "error", {}

    try:
        parsed = parse_fit(fit, zone(tz_name), source)
        counts = store_parsed(conn, parsed, stored.raw_file_id)
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        archive.mark_parsed(stored.raw_file_id, status="error", error=f"{type(exc).__name__}: {exc}")
        conn.commit()
        return "error", {}

    archive.mark_parsed(
        stored.raw_file_id,
        status="ok",
        fit_file_type=fit.file_type,
        fit_time_created_utc=fit.time_created_iso,
        device_serial=str(fit.serial_number) if fit.serial_number else None,
        device_product=fit.product_name or fit.product,
    )
    conn.commit()
    return "new", counts


def locate_garmin(
    config: Config,
    explicit: Path | None = None,
    *,
    roots: Callable[[], list[Path]] | None = None,
    mounter: Callable[[], tuple[bool, str]] = try_mount_garmin,
) -> tuple[Path | None, str | None]:
    """Find the GARMIN folder; without --path, try to gio-mount an attached MTP device first.

    ``roots`` yields the directories to search (default: the system mount points);
    tests inject both ``roots`` and ``mounter`` so they never touch a real device.
    """
    if explicit is not None:
        return garmin_dir(explicit) or (explicit if explicit.is_dir() else None), None
    search = roots or (lambda: candidate_roots(extra=config.garmin_mount_hints))
    root = find_garmin_root(search())
    if root is not None:
        return root, None
    mounted, message = mounter()
    if mounted:
        root = find_garmin_root(search())
    return root, message


def import_device_files(
    conn: sqlite3.Connection,
    config: Config,
    files: list[DeviceFile],
    *,
    dry_run: bool = False,
    log: Log = lambda _msg: None,
    stop: Callable[[], bool] | None = None,
    stats: ImportStats | None = None,
) -> ImportStats:
    """Archive and parse the given files (whatever transport brought them); ``stop`` is polled between files."""
    stats = stats if stats is not None else ImportStats()
    stats.found += len(files)
    archive = RawArchive(config.raw_dir, conn)
    for df in files:
        if stop is not None and stop():
            stats.stopped = True
            log("  stopped on request")
            break
        try:
            data = df.path.read_bytes()
        except OSError as exc:
            stats.errors += 1
            stats.error_files.append((df.origin or str(df.path), str(exc)))
            log(f"  ! {df.folder}/{df.name}: {exc}")
            continue
        if dry_run:
            if archive.lookup(sha256_bytes(data)) is None:
                stats.new += 1
                log(f"  + {df.folder}/{df.name} ({df.size} bytes) would be imported")
            else:
                stats.skipped += 1
            continue
        status, counts = import_file(
            conn,
            archive,
            data,
            kind=df.kind,
            original_name=df.name,
            origin_path=df.origin or str(df.path),
            device_mtime=df.mtime,
            tz_name=config.timezone,
        )
        if status == "new":
            stats.new += 1
            stats.parsed_ok += 1
            stats.add_rows(counts)
            summary = ", ".join(f"{k}={v}" for k, v in counts.items()) or "no rows"
            log(f"  + {df.folder}/{df.name}: {summary}")
        elif status == "skipped":
            stats.skipped += 1
        else:
            stats.new += 1
            stats.errors += 1
            err = conn.execute("SELECT parse_error FROM raw_files WHERE sha256 = ?", (sha256_bytes(data),)).fetchone()
            stats.error_files.append((df.origin or str(df.path), err[0] if err else "unknown"))
            log(f"  ! {df.folder}/{df.name}: {err[0] if err else 'unknown error'}")
    if not dry_run and stats.parsed_ok:
        rebuild_daily_summary(conn, source=SOURCE)
        conn.commit()
    return stats


def import_garmin_mtp(
    conn: sqlite3.Connection,
    config: Config,
    *,
    mtp: Mtp,
    dry_run: bool = False,
    log: Log = lambda _msg: None,
    stop: Callable[[], bool] | None = None,
) -> ImportStats:
    """Import over the MTP tools: list the configured folders, skip files already archived by name and
    size, download the rest into ``<data_dir>/staging/`` and archive them from there.

    The staging copy is removed afterwards; ``origin_path`` records ``mtp://GARMIN/<Folder>/<name>`` and
    ``device_mtime_utc`` stays empty (the tools report no modification time). A dry run downloads nothing:
    it reports what the name+size prefilter would fetch.
    """
    stats = ImportStats()
    info, stats.mount_message = probe_mtp(mtp)
    if info is None:
        return stats
    stats.root = MTP_ROOT_LABEL
    log(f"  {stats.mount_message}")
    known = RawArchive(config.raw_dir, conn).known_name_sizes(SOURCE)
    todo: list[tuple[str, str, object]] = []  # (folder as the device spells it, kind, MtpFile)
    try:
        folders = mtp.folders()
        for folder in config.garmin_folders:
            hit = folder_id(folders, f"{GARMIN_ROOT}/{folder}")
            if hit is None:
                continue
            spelled = hit[1].rsplit("/", 1)[-1]
            kind = FOLDER_KINDS.get(spelled.lower(), "other")
            for entry in mtp.list(hit[1]):
                if not entry.name.lower().endswith(".fit"):
                    continue
                stats.found += 1
                if (kind, entry.name, entry.size) in known:
                    stats.skipped += 1
                else:
                    todo.append((spelled, kind, entry))
    except MtpError as exc:
        stats.errors += 1
        stats.error_files.append((MTP_ROOT_LABEL, str(exc)))
        log(f"  ! listing failed: {exc}")
        return stats
    if stats.skipped:
        log(f"  {stats.skipped} already archived (same name and size)")
    if dry_run:
        for spelled, _kind, entry in todo:
            stats.new += 1
            log(f"  + {spelled}/{entry.name} ({entry.size} bytes) would be fetched")
        return stats
    if not todo:
        return stats
    staging_root = config.data_dir / "staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="garmin-", dir=staging_root))
    try:
        files: list[DeviceFile] = []
        for spelled, kind, entry in todo:
            if stop is not None and stop():
                stats.stopped = True
                log("  stopped on request")
                break
            dest = staging / spelled / entry.name
            try:
                mtp.get(entry.id, dest)  # type: ignore[attr-defined]
            except NoMtpDeviceError as exc:
                stats.errors += 1
                stats.error_files.append((f"{MTP_ROOT_LABEL}/{spelled}/{entry.name}", str(exc)))
                log(f"  ! {spelled}/{entry.name}: {exc}; the watch is gone, importing what was fetched")
                break
            except MtpError as exc:
                stats.errors += 1
                stats.error_files.append((f"{MTP_ROOT_LABEL}/{spelled}/{entry.name}", str(exc)))
                log(f"  ! {spelled}/{entry.name}: {exc}")
                continue
            stats.fetched += 1
            files.append(DeviceFile(path=dest, kind=kind, folder=spelled, name=entry.name, size=entry.size, mtime=None, origin=f"{MTP_ROOT_LABEL}/{spelled}/{entry.name}"))  # type: ignore[attr-defined]
        if files:
            log(f"  fetched {len(files)} files over MTP")
            found_before = stats.found
            import_device_files(conn, config, files, dry_run=False, log=log, stop=None, stats=stats)
            stats.found = found_before  # counted at listing time already
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return stats


def import_garmin(
    conn: sqlite3.Connection,
    config: Config,
    *,
    path: Path | None = None,
    dry_run: bool = False,
    log: Log = lambda _msg: None,
    roots: Callable[[], list[Path]] | None = None,
    mounter: Callable[[], tuple[bool, str]] = try_mount_garmin,
    stop: Callable[[], bool] | None = None,
    access: str | None = None,
    mtp_factory: MtpFactory | None = None,
) -> ImportStats:
    """Find the watch, copy every new FIT file into the archive and parse it.

    An explicit ``path`` is imported as a directory. Otherwise ``access`` (default: ``[garmin] access``
    resolved by :func:`wristpull.mtp.resolve_access`) decides between the mounted GARMIN folder
    (``roots``/``mounter``) and the MTP tools (``mtp_factory``, default :class:`MtpTools`).
    ``stop`` is polled between files; when it returns True the import ends early with
    ``stats.stopped`` set (files already imported stay imported).
    """
    if path is None and (access or resolve_access(config)) == "mtp":
        return import_garmin_mtp(conn, config, mtp=(mtp_factory or MtpTools)(), dry_run=dry_run, log=log, stop=stop)
    stats = ImportStats()
    root, stats.mount_message = locate_garmin(config, path, roots=roots, mounter=mounter)
    if root is None:
        return stats
    if stats.mount_message:
        log(f"  {stats.mount_message}")
    stats.root = root
    files: list[DeviceFile] = list_fit_files(root, config.garmin_folders)
    return import_device_files(conn, config, files, dry_run=dry_run, log=log, stop=stop, stats=stats)
    if stats.mount_message:
        log(f"  {stats.mount_message}")
    stats.root = root
    files: list[DeviceFile] = list_fit_files(root, config.garmin_folders)
    stats.found = len(files)
    archive = RawArchive(config.raw_dir, conn)
    for df in files:
        if stop is not None and stop():
            stats.stopped = True
            log("  stopped on request")
            break
        try:
            data = df.path.read_bytes()
        except OSError as exc:
            stats.errors += 1
            stats.error_files.append((str(df.path), str(exc)))
            log(f"  ! {df.folder}/{df.name}: {exc}")
            continue
        if dry_run:
            if archive.lookup(sha256_bytes(data)) is None:
                stats.new += 1
                log(f"  + {df.folder}/{df.name} ({df.size} bytes) would be imported")
            else:
                stats.skipped += 1
            continue
        status, counts = import_file(
            conn,
            archive,
            data,
            kind=df.kind,
            original_name=df.name,
            origin_path=str(df.path),
            device_mtime=df.mtime,
            tz_name=config.timezone,
        )
        if status == "new":
            stats.new += 1
            stats.parsed_ok += 1
            stats.add_rows(counts)
            summary = ", ".join(f"{k}={v}" for k, v in counts.items()) or "no rows"
            log(f"  + {df.folder}/{df.name}: {summary}")
        elif status == "skipped":
            stats.skipped += 1
        else:
            stats.new += 1
            stats.errors += 1
            err = conn.execute("SELECT parse_error FROM raw_files WHERE sha256 = ?", (sha256_bytes(data),)).fetchone()
            stats.error_files.append((str(df.path), err[0] if err else "unknown"))
            log(f"  ! {df.folder}/{df.name}: {err[0] if err else 'unknown error'}")
    if not dry_run and stats.parsed_ok:
        rebuild_daily_summary(conn, source=SOURCE)
        conn.commit()
    return stats
