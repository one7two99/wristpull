"""Re-parse archived raw files after a parser change: delete their rows, parse again, store."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..archive import RawArchive
from ..config import Config
from ..daily import rebuild_daily_summary
from ..fit.decode import decode_fit
from ..fit.garmin import parse_fit
from ..gadgetbridge import parse_gadgetbridge_db
from ..rows import SAMPLE_TABLES
from ..timeutil import zone
from .garmin import store_parsed

Log = Callable[[str], None]


@dataclass
class ReparseStats:
    selected: int = 0
    ok: int = 0
    errors: int = 0
    deleted: int = 0
    rows: dict[str, int] = field(default_factory=dict)
    sources: set[str] = field(default_factory=set)
    error_files: list[tuple[str, str]] = field(default_factory=list)

    def add_rows(self, counts: dict[str, int]) -> None:
        for key, value in counts.items():
            self.rows[key] = self.rows.get(key, 0) + value


JSON_KINDS = ("uds", "sleep_json")  # Garmin Connect export files parsed by garmin_connect.py


def sources_of(parsed) -> list[str]:
    """Distinct sources that actually have rows in a ParsedRows."""
    found = {a.row["source"] for a in parsed.activities} | {n.row["source"] for n in parsed.sleep}
    for table in SAMPLE_TABLES:
        found.update(r["source"] for r in getattr(parsed, table))
    return sorted(found)


def delete_rows_of(conn: sqlite3.Connection, raw_file_id: int) -> int:
    """Remove everything that was parsed from one raw file (activities with records/laps, sleep with stages, samples)."""
    deleted = 0
    for parent, child in (("activities", "activity_records"), ("activities", "activity_laps"), ("sleep", "sleep_stages")):
        key = "activity_id" if parent == "activities" else "sleep_id"
        deleted += conn.execute(
            f"DELETE FROM {child} WHERE {key} IN (SELECT id FROM {parent} WHERE raw_file_id = ?)", (raw_file_id,)
        ).rowcount
    for table in ("activities", "sleep", "connect_daily", *SAMPLE_TABLES):
        deleted += conn.execute(f"DELETE FROM {table} WHERE raw_file_id = ?", (raw_file_id,)).rowcount
    return deleted


def select_raw_files(
    conn: sqlite3.Connection,
    *,
    ids: list[int] | None = None,
    source: str | None = None,
    kind: str | None = None,
    errors_only: bool = False,
) -> list[sqlite3.Row]:
    clauses, params = [], []
    if ids:
        clauses.append(f"id IN ({','.join('?' * len(ids))})")
        params.extend(ids)
    if source:
        clauses.append("source = ?")
        params.append(source)
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if errors_only:
        clauses.append("parse_status = 'error'")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return conn.execute(f"SELECT * FROM raw_files{where} ORDER BY id", params).fetchall()


def reparse_raw_file(conn: sqlite3.Connection, config: Config, raw_file: sqlite3.Row, *, log: Log = lambda _m: None) -> tuple[bool, int, dict[str, int], list[str]]:
    """Delete and re-create the rows of one archived file. Returns (ok, deleted, counts, sources)."""
    archive = RawArchive(config.raw_dir, conn)
    raw_file_id = int(raw_file["id"])
    path = config.raw_dir / raw_file["archive_path"]
    tz = zone(config.timezone)
    deleted = delete_rows_of(conn, raw_file_id)
    conn.commit()  # a failed parse leaves the file without rows and marked 'error', never with stale rows
    try:
        if raw_file["kind"] == "database":
            parsed, _devices = parse_gadgetbridge_db(path, tz)
            sources = sources_of(parsed)
            counts = store_parsed(conn, parsed, raw_file_id)
            archive.mark_parsed(raw_file_id, status="ok", fit_file_type="gadgetbridge_db")
        elif raw_file["kind"] in JSON_KINDS:
            from .connect import store_json

            counts, _discarded = store_json(conn, path.read_bytes(), kind=raw_file["kind"], raw_file_id=raw_file_id, source=raw_file["source"], tz_name=config.timezone)
            sources = [raw_file["source"]]
            archive.mark_parsed(raw_file_id, status="ok", fit_file_type=raw_file["kind"])
        else:
            fit = decode_fit(path)
            parsed = parse_fit(fit, tz, raw_file["source"])
            sources = sources_of(parsed)
            counts = store_parsed(conn, parsed, raw_file_id)
            archive.mark_parsed(
                raw_file_id,
                status="ok",
                fit_file_type=fit.file_type,
                fit_time_created_utc=fit.time_created_iso,
                device_serial=str(fit.serial_number) if fit.serial_number else None,
                device_product=fit.product_name or fit.product,
            )
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
        conn.rollback()
        archive.mark_parsed(raw_file_id, status="error", error=f"{type(exc).__name__}: {exc}")
        conn.commit()
        log(f"  ! {raw_file['archive_path']}: {type(exc).__name__}: {exc}")
        return False, deleted, {}, []
    conn.commit()
    log(f"  ~ {raw_file['archive_path']}: -{deleted} rows, +" + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing"))
    return True, deleted, counts, sources


def reparse(
    conn: sqlite3.Connection,
    config: Config,
    *,
    ids: list[int] | None = None,
    source: str | None = None,
    kind: str | None = None,
    errors_only: bool = False,
    log: Log = lambda _m: None,
) -> ReparseStats:
    """Re-parse the selected archived files and rebuild the daily summaries of the touched sources."""
    stats = ReparseStats()
    for raw_file in select_raw_files(conn, ids=ids, source=source, kind=kind, errors_only=errors_only):
        stats.selected += 1
        if not (config.raw_dir / raw_file["archive_path"]).exists():
            stats.errors += 1
            stats.error_files.append((raw_file["archive_path"], "missing in the archive"))
            log(f"  ! {raw_file['archive_path']}: missing in the archive")
            continue
        ok, deleted, counts, sources = reparse_raw_file(conn, config, raw_file, log=log)
        stats.deleted += deleted
        if ok:
            stats.ok += 1
            stats.add_rows(counts)
            stats.sources.update(sources)
        else:
            stats.errors += 1
            stats.error_files.append((raw_file["archive_path"], "parse error"))
    for src in sorted(stats.sources):
        rebuild_daily_summary(conn, source=src)
    conn.commit()
    return stats


def archive_path_of(config: Config, raw_file: sqlite3.Row) -> Path:
    return config.raw_dir / raw_file["archive_path"]
