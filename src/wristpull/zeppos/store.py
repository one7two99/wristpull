"""SQL for the BLE pull: where each fetch kind left off, and nights the band re-reported."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from ..timeutil import iso_utc, now_utc, parse_iso_utc


def last_sync(conn: sqlite3.Connection, source: str, kind: str) -> datetime | None:
    row = conn.execute("SELECT last_utc FROM strap_sync WHERE source = ? AND kind = ?", (source, kind)).fetchone()
    return parse_iso_utc(row[0]) if row else None


def all_sync(conn: sqlite3.Connection, source: str) -> dict[str, datetime]:
    rows = conn.execute("SELECT kind, last_utc FROM strap_sync WHERE source = ? ORDER BY kind", (source,)).fetchall()
    return {str(r[0]): parse_iso_utc(r[1]) for r in rows}


def set_last_sync(conn: sqlite3.Connection, source: str, kind: str, last_utc: datetime) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO strap_sync (source, kind, last_utc, updated_at_utc) VALUES (?, ?, ?, ?)",
        (source, kind, iso_utc(last_utc), iso_utc(now_utc())),
    )


def clear_sync(conn: sqlite3.Connection, source: str) -> int:
    return conn.execute("DELETE FROM strap_sync WHERE source = ?", (source,)).rowcount


def delete_overlapping_sleep(conn: sqlite3.Connection, source: str, start_utc: str, end_utc: str) -> int:
    """Remove nights of ``source`` that overlap ``[start_utc, end_utc)``: the band re-reports a night
    when it extends it, and the newer report replaces the older one (as Gadgetbridge does)."""
    ids = [r[0] for r in conn.execute("SELECT id FROM sleep WHERE source = ? AND start_utc < ? AND end_utc > ?", (source, end_utc, start_utc))]
    for sleep_id in ids:
        conn.execute("DELETE FROM sleep_stages WHERE sleep_id = ?", (sleep_id,))
        conn.execute("DELETE FROM sleep WHERE id = ?", (sleep_id,))
    return len(ids)
