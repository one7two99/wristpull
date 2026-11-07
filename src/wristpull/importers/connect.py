"""Import the Garmin Connect account export (the GDPR data export, "DI_CONNECT").

Only ``DI_CONNECT/DI-Connect-Uploaded-Files/UploadedFiles_*.zip`` matters in phase 1: every FIT
file the watches ever uploaded to Connect (``monitoring_b``, sleep, HRV, metrics, activities).
The portal zip is read in place with :mod:`zipfile`, the inner zips from memory, file by file,
so nothing is extracted to disk; an already extracted directory works the same way. Files are
archived under the source ``garmin_connect`` through :func:`wristpull.importers.garmin.import_file`,
which skips bytes already archived from the watch (SHA-256) and derives ``kind`` from the FIT type.
"""

from __future__ import annotations

import io
import sqlite3
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from ..archive import RawArchive, sha256_bytes
from ..config import Config
from ..daily import rebuild_daily_summary
from .garmin import ImportStats, import_file

SOURCE = "garmin_connect"
UPLOADS_DIR = "DI-Connect-Uploaded-Files"
Log = Callable[[str], None]


@dataclass(frozen=True)
class ExportFile:
    name: str  # file name without the account prefix, e.g. "7102332115.fit"
    origin: str  # where it came from, e.g. "UploadedFiles_0-_Part1.zip/7102332115.fit"
    data: bytes


def display_name(member: str) -> str:
    """The member's base name without the ``<e-mail>_`` prefix Connect puts in front of every upload."""
    base = member.rsplit("/", 1)[-1]
    if "@" in base and "_" in base:
        return base.rsplit("_", 1)[-1]
    return base


def _is_fit(name: str) -> bool:
    return name.lower().endswith(".fit")


def _is_uploads_zip(name: str) -> bool:
    parts = name.replace("\\", "/").split("/")
    return name.lower().endswith(".zip") and UPLOADS_DIR in parts[:-1]


def _fit_members(inner: zipfile.ZipFile, label: str) -> Iterator[ExportFile]:
    for info in inner.infolist():
        if info.is_dir() or not _is_fit(info.filename):
            continue
        yield ExportFile(display_name(info.filename), f"{label}/{display_name(info.filename)}", inner.read(info))


def iter_export_fit(path: Path) -> Iterator[ExportFile]:
    """Every FIT file of an export: ``path`` is the portal zip, an ``UploadedFiles_*.zip`` or an
    extracted directory (searched for the uploads folder, zips inside it, and loose ``.fit`` files)."""
    path = Path(path)
    if path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as outer:
            names = outer.namelist()
            inner_zips = sorted(n for n in names if _is_uploads_zip(n))
            if not inner_zips and any(_is_fit(n) for n in names):  # the inner zip itself
                yield from _fit_members(outer, path.name)
                return
            for name in inner_zips:
                with zipfile.ZipFile(io.BytesIO(outer.read(name))) as inner:
                    yield from _fit_members(inner, name.rsplit("/", 1)[-1])
        return
    if not path.is_dir():
        raise FileNotFoundError(f"{path} is neither a zip file nor a directory")
    uploads = next((p for p in path.rglob(UPLOADS_DIR) if p.is_dir()), path)
    for zip_path in sorted(p for p in uploads.rglob("*.zip") if p.is_file()):
        with zipfile.ZipFile(zip_path) as inner:
            yield from _fit_members(inner, zip_path.name)
    for fit_path in sorted(p for p in uploads.rglob("*") if p.is_file() and _is_fit(p.name)):
        yield ExportFile(display_name(fit_path.name), f"{uploads.name}/{display_name(fit_path.name)}", fit_path.read_bytes())


def import_garmin_export(
    conn: sqlite3.Connection,
    config: Config,
    path: Path,
    *,
    dry_run: bool = False,
    log: Log = lambda _m: None,
    stop: Callable[[], bool] | None = None,
    progress_every: int = 500,
) -> ImportStats:
    """Archive and parse every FIT file of the export under the source ``garmin_connect``.

    One commit per file (a crash never leaves an archived file without its ``raw_files`` row);
    ``log`` gets a progress line every ``progress_every`` files. ``stop`` is polled between files.
    """
    stats = ImportStats(root=str(path))
    archive = RawArchive(config.raw_dir, conn)
    for item in iter_export_fit(path):
        if stop is not None and stop():
            stats.stopped = True
            log("  stopped on request")
            break
        stats.found += 1
        if dry_run:
            if archive.lookup(sha256_bytes(item.data)) is None:
                stats.new += 1
            else:
                stats.skipped += 1
        else:
            status, counts = import_file(conn, archive, item.data, kind=None, original_name=item.name, origin_path=item.origin, device_mtime=None, tz_name=config.timezone, source=SOURCE)
            if status == "new":
                stats.new += 1
                stats.parsed_ok += 1
                stats.add_rows(counts)
            elif status == "skipped":
                stats.skipped += 1
            else:
                stats.new += 1
                stats.errors += 1
                err = conn.execute("SELECT parse_error FROM raw_files WHERE sha256 = ?", (sha256_bytes(item.data),)).fetchone()
                stats.error_files.append((item.origin, err[0] if err else "unknown"))
        if progress_every and stats.found % progress_every == 0:
            log(f"  {stats.found} files: {stats.new} new, {stats.skipped} already archived, {stats.errors} errors")
    if not dry_run and stats.parsed_ok:
        log("  rebuilding daily summaries …")
        rebuild_daily_summary(conn, source=SOURCE)
        conn.commit()
    return stats
