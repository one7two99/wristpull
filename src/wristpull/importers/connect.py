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
from datetime import timedelta
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from ..archive import RawArchive, sha256_bytes
from ..config import Config
from ..daily import rebuild_daily_summary
from ..db import insert_rows
from ..garmin_connect import parse_activity_summaries, parse_maxmet, parse_sleep_json, parse_uds, sport_matches
from ..timeutil import iso_utc, parse_iso_utc
from .garmin import ImportStats, import_file, store_parsed

SOURCE = "garmin_connect"
UPLOADS_DIR = "DI-Connect-Uploaded-Files"
# JSON files of the export and their raw_files.kind: daily summaries and nights (phase 2)
JSON_FILES = (
    ("DI-Connect-Aggregator", "UDSFile_", "uds"),
    ("DI-Connect-Wellness", "_sleepData.json", "sleep_json"),
    ("DI-Connect-Fitness", "summarizedActivities", "activities_json"),
    ("DI-Connect-Metrics", "MetricsMaxMetData_", "maxmet"),
)
MATCH_TOLERANCE_S = 120  # a Connect activity and an archived one belong together when they start within this window
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


def _json_kind(name: str) -> str | None:
    parts = name.replace("\\", "/").split("/")
    base = parts[-1]
    for folder, marker, kind in JSON_FILES:
        if folder in parts[:-1] and marker in base and base.lower().endswith(".json"):
            return kind
    return None


def iter_export_json(path: Path) -> Iterator[tuple[str, ExportFile]]:
    """``(kind, file)`` for the daily-summary and sleep JSON files, sorted by name so that later
    date blocks are processed last (they win on overlapping days)."""
    path = Path(path)
    found: list[tuple[str, str, Callable[[], bytes]]] = []
    if path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as outer:
            for name in outer.namelist():
                kind = _json_kind(name)
                if kind:
                    found.append((kind, name, (lambda n=name: outer.read(n))))
            for kind, name, read in sorted(found, key=lambda f: f[1]):
                yield kind, ExportFile(name.rsplit("/", 1)[-1], name.split("DI_CONNECT/")[-1], read())
        return
    if path.is_dir():
        for file in sorted(p for p in path.rglob("*.json") if p.is_file()):
            kind = _json_kind(file.as_posix())
            if kind:
                yield kind, ExportFile(file.name, file.relative_to(path).as_posix(), file.read_bytes())


def store_json(conn: sqlite3.Connection, data: bytes, *, kind: str, raw_file_id: int, source: str, tz_name: str) -> tuple[dict[str, int], int]:
    """Parse one archived JSON file into the tables; returns (inserted counts, discarded records)."""
    if kind == "uds":
        rows, discarded = parse_uds(data, source=source, tz_name=tz_name)
        n = insert_rows(conn, "connect_daily", ({**r, "raw_file_id": raw_file_id} for r in rows), ignore=False, replace=True)
        return ({"connect_daily": n} if n else {}), discarded
    if kind == "sleep_json":
        parsed, discarded = parse_sleep_json(data, source=source, tz_name=tz_name)
        # a night the FIT files already delivered (with stages) wins over the export's summary of it
        known = {r[0] for r in conn.execute("SELECT local_date FROM sleep WHERE source = ? AND raw_file_id != ?", (source, raw_file_id))}
        parsed.sleep = [n for n in parsed.sleep if n.row["local_date"] not in known]
        return store_parsed(conn, parsed, raw_file_id), discarded
    if kind == "activities_json":
        named = enrich_activity_names(conn, parse_activity_summaries(data))
        return ({"activity_names": named} if named else {}), 0
    if kind == "maxmet":
        n = store_vo2max_history(conn, parse_maxmet(data, source=source, tz_name=tz_name), raw_file_id=raw_file_id)
        return ({"metrics": n} if n else {}), 0
    raise ValueError(f"unknown JSON kind {kind}")


def enrich_activity_names(conn: sqlite3.Connection, summaries: list[dict]) -> int:
    """Give archived activities (any source) the name Connect knows: same start within
    ``MATCH_TOLERANCE_S`` and a compatible sport; only where ``name`` is still NULL. Returns the rows named."""
    named = 0
    for item in summaries:
        start = parse_iso_utc(item["start_utc"])
        lo, hi = iso_utc(start - timedelta(seconds=MATCH_TOLERANCE_S)), iso_utc(start + timedelta(seconds=MATCH_TOLERANCE_S))
        rows = conn.execute("SELECT id, start_utc, sport, name FROM activities WHERE start_utc BETWEEN ? AND ?", (lo, hi)).fetchall()
        candidates = [r for r in rows if sport_matches(item["activity_type"], r["sport"])]
        if not candidates:
            continue
        for r in candidates:
            if r["name"] is None:
                conn.execute("UPDATE activities SET name = ? WHERE id = ?", (item["name"], r["id"]))
                named += 1
    return named


def store_vo2max_history(conn: sqlite3.Connection, rows: list[dict], *, raw_file_id: int) -> int:
    """VO2max values from the export's metrics files; a day that already has one from a FIT file keeps it."""
    n = 0
    for row in rows:
        exists = conn.execute("SELECT 1 FROM metrics WHERE source = ? AND local_date = ? AND metric = 'vo2_max'", (row["source"], row["local_date"])).fetchone()
        if exists:
            continue
        n += insert_rows(conn, "metrics", [{**row, "raw_file_id": raw_file_id}])
    return n


def import_json_file(conn: sqlite3.Connection, archive: RawArchive, item: ExportFile, *, kind: str, tz_name: str, source: str = SOURCE) -> tuple[str, dict[str, int], int]:
    """Archive + parse one JSON file. Returns ('new'|'skipped'|'error', counts, discarded)."""
    if archive.lookup(sha256_bytes(item.data)) is not None:
        return "skipped", {}, 0
    year = item.name[len("UDSFile_"):len("UDSFile_") + 4] if kind == "uds" else item.name[:4]
    if kind in ("activities_json", "maxmet"):
        year = next((part[:4] for part in item.name.split("_") if part[:4].isdigit() and len(part) >= 8), "all")
    stored = archive.store(item.data, source=source, kind=kind, original_name=item.name, origin_path=item.origin, year=year if year.isdigit() else None)
    try:
        counts, discarded = store_json(conn, item.data, kind=kind, raw_file_id=stored.raw_file_id, source=source, tz_name=tz_name)
    except Exception as exc:  # noqa: BLE001 - a changed export format must not stop the run
        conn.rollback()
        archive.mark_parsed(stored.raw_file_id, status="error", error=f"{type(exc).__name__}: {exc}")
        conn.commit()
        return "error", {}, 0
    archive.mark_parsed(stored.raw_file_id, status="ok", fit_file_type=kind)
    conn.commit()
    return "new", counts, discarded


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
    if not stats.stopped:
        _import_json(conn, config, path, archive, stats, dry_run=dry_run, log=log)
    if not dry_run and stats.parsed_ok:
        log("  rebuilding daily summaries …")
        rebuild_daily_summary(conn, source=SOURCE)
        conn.commit()
    return stats


def _import_json(conn: sqlite3.Connection, config: Config, path: Path, archive: RawArchive, stats: ImportStats, *, dry_run: bool, log: Log) -> None:
    """Daily summaries and nights from the export's JSON files (kinds ``uds`` and ``sleep_json``)."""
    counts: dict[str, int] = {"found": 0, "new": 0, "skipped": 0, "errors": 0}
    for kind, item in iter_export_json(path):
        counts["found"] += 1
        stats.found += 1
        if dry_run:
            if archive.lookup(sha256_bytes(item.data)) is None:
                stats.new += 1
                counts["new"] += 1
            else:
                stats.skipped += 1
                counts["skipped"] += 1
            continue
        status, inserted, discarded = import_json_file(conn, archive, item, kind=kind, tz_name=config.timezone)
        stats.discarded += discarded
        if status == "new":
            stats.new += 1
            stats.parsed_ok += 1
            stats.add_rows(inserted)
            counts["new"] += 1
        elif status == "skipped":
            stats.skipped += 1
            counts["skipped"] += 1
        else:
            stats.new += 1
            stats.errors += 1
            counts["errors"] += 1
            err = conn.execute("SELECT parse_error FROM raw_files WHERE sha256 = ?", (sha256_bytes(item.data),)).fetchone()
            stats.error_files.append((item.origin, err[0] if err else "unknown"))
    if counts["found"]:
        log(f"  JSON: {counts['found']} files (daily summaries, sleep, activity names, VO2max), {counts['new']} new, {counts['skipped']} already archived, {counts['errors']} errors" + (f", {stats.discarded} placeholder records discarded" if stats.discarded else ""))
