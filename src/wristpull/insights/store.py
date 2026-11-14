"""SQL for the day tags (table ``day_tags``); the only SQL in the insights package."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from .tags import valid


def tags_between(conn: sqlite3.Connection, start: str, end: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for row in conn.execute("SELECT local_date, tag FROM day_tags WHERE local_date BETWEEN ? AND ? ORDER BY local_date, tag", (start, end)):
        out.setdefault(row["local_date"], set()).add(row["tag"])
    return out


def tags_for(conn: sqlite3.Connection, local_date: str) -> list[str]:
    return [r["tag"] for r in conn.execute("SELECT tag FROM day_tags WHERE local_date = ? ORDER BY tag", (local_date,))]


def set_tags(conn: sqlite3.Connection, local_date: str, *, add: list[str] = (), remove: list[str] = ()) -> list[str]:
    """Add and remove tags of one day (unknown tags are ignored); returns the day's tags afterwards."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for tag in add:
        if valid(tag):
            conn.execute("INSERT OR IGNORE INTO day_tags (local_date, tag, created_at_utc) VALUES (?, ?, ?)", (local_date, tag, stamp))
    for tag in remove:
        conn.execute("DELETE FROM day_tags WHERE local_date = ? AND tag = ?", (local_date, tag))
    conn.commit()
    return tags_for(conn, local_date)


def replace_tags(conn: sqlite3.Connection, local_date: str, tags: list[str]) -> list[str]:
    """The day's tags become exactly ``tags`` (the checkbox form of the dashboard)."""
    current = set(tags_for(conn, local_date))
    wanted = {t for t in tags if valid(t)}
    return set_tags(conn, local_date, add=sorted(wanted - current), remove=sorted(current - wanted))
