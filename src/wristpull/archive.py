"""Immutable raw-file archive with SHA-256 de-duplication."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .db import insert_row
from .timeutil import iso_utc, now_utc

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_name(name: str) -> str:
    cleaned = _SAFE_NAME.sub("_", name).strip("._")
    return cleaned or "file"


@dataclass(frozen=True)
class StoredFile:
    raw_file_id: int
    sha256: str
    archive_path: Path  # absolute
    is_new: bool


class RawArchive:
    """Stores files under <raw_dir>/<source>/<year>/<kind>/<sha8>_<name>.

    Files are never overwritten and never deleted. Identity is the SHA-256 of the
    content, tracked in the ``raw_files`` table.
    """

    def __init__(self, raw_dir: Path, conn: sqlite3.Connection) -> None:
        self.raw_dir = Path(raw_dir)
        self.conn = conn

    def lookup(self, sha256: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM raw_files WHERE sha256 = ?", (sha256,)).fetchone()

    def lookup_path(self, path: Path | str) -> sqlite3.Row | None:
        """Find the raw_files row for an archived file given its absolute or raw-relative path."""
        candidate = Path(path)
        if candidate.is_absolute():
            try:
                candidate = candidate.resolve().relative_to(self.raw_dir.resolve())
            except ValueError:
                return None
        return self.conn.execute("SELECT * FROM raw_files WHERE archive_path = ?", (candidate.as_posix(),)).fetchone()

    def store(
        self,
        data: bytes,
        *,
        source: str,
        kind: str,
        original_name: str,
        origin_path: str | None = None,
        device_mtime: datetime | None = None,
        year: int | str | None = None,
    ) -> StoredFile:
        digest = sha256_bytes(data)
        existing = self.lookup(digest)
        if existing is not None:
            return StoredFile(int(existing["id"]), digest, self.raw_dir / existing["archive_path"], False)

        year_part = str(year) if year is not None else (device_mtime.strftime("%Y") if device_mtime else "unknown")
        rel = Path(source) / year_part / kind / f"{digest[:8]}_{safe_name(original_name)}"
        target = self.raw_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if sha256_bytes(target.read_bytes()) != digest:
                raise FileExistsError(f"refusing to overwrite {target} with different content")
        else:
            with target.open("xb") as fh:
                fh.write(data)
            target.chmod(0o444)

        raw_id = insert_row(
            self.conn,
            "raw_files",
            {
                "sha256": digest,
                "source": source,
                "kind": kind,
                "original_name": original_name,
                "origin_path": origin_path,
                "archive_path": rel.as_posix(),
                "size_bytes": len(data),
                "device_mtime_utc": iso_utc(device_mtime) if device_mtime else None,
                "imported_at_utc": iso_utc(now_utc()),
            },
        )
        return StoredFile(raw_id, digest, target, True)

    def mark_parsed(self, raw_file_id: int, *, status: str, error: str | None = None, **fit_info) -> None:
        cols = {
            "parsed_at_utc": iso_utc(now_utc()),
            "parse_status": status,
            "parse_error": error,
        }
        for key in ("fit_file_type", "fit_time_created_utc", "device_serial", "device_product"):
            if key in fit_info and fit_info[key] is not None:
                cols[key] = fit_info[key]
        assignments = ", ".join(f"{k} = ?" for k in cols)
        self.conn.execute(f"UPDATE raw_files SET {assignments} WHERE id = ?", [*cols.values(), raw_file_id])
