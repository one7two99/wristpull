"""SQLite access: connection, schema, generic insert helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = "4"  # 2: coach_runs, coach_feedback, journal; 3: workout_pushes; 4: connect_daily


def connect(path: Path | str) -> sqlite3.Connection:
    """Open (and create) the database with the schema applied."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL") if str(path) != ":memory:" else None
    apply_schema(conn)
    return conn


# Columns added after the first release; applied to existing databases on connect.
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("daily_summary", "temperature_avg", "REAL"),
)


def ensure_columns(conn: sqlite3.Connection, migrations=MIGRATIONS) -> list[str]:
    """Add missing columns listed in ``migrations``; returns the columns added."""
    added: list[str] = []
    for table, column, decl in migrations:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if existing and column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            added.append(f"{table}.{column}")
    return added


def apply_schema(conn: sqlite3.Connection) -> None:
    sql = resources.files("wristpull").joinpath("schema.sql").read_text("utf-8")
    conn.executescript(sql)
    ensure_columns(conn)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()


def insert_rows(
    conn: sqlite3.Connection,
    table: str,
    rows: Iterable[Mapping[str, object]],
    *,
    ignore: bool = True,
    replace: bool = False,
) -> int:
    """Insert dict rows; returns the number of rows actually inserted.

    With ``ignore`` duplicates on UNIQUE constraints are skipped silently so
    overlapping device files can be imported idempotently; ``replace`` instead
    overwrites the existing row (for derived values that may be recomputed).
    """
    rows = list(rows)
    if not rows:
        return 0
    verb = "INSERT OR REPLACE" if replace else ("INSERT OR IGNORE" if ignore else "INSERT")
    inserted = 0
    for row in rows:
        cols = list(row.keys())
        sql = f"{verb} INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
        cur = conn.execute(sql, [row[c] for c in cols])
        inserted += cur.rowcount if cur.rowcount > 0 else 0
    return inserted


def insert_row(conn: sqlite3.Connection, table: str, row: Mapping[str, object]) -> int:
    """Insert one row and return its rowid (raises on conflict)."""
    cols = list(row.keys())
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    cur = conn.execute(sql, [row[c] for c in cols])
    return int(cur.lastrowid)


def table_count(conn: sqlite3.Connection, table: str, where: str = "", params: tuple = ()) -> int:
    sql = f"SELECT COUNT(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return int(conn.execute(sql, params).fetchone()[0])


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()):
    row = conn.execute(sql, params).fetchone()
    return None if row is None else row[0]
