"""Workout files under <data_dir>/workouts/ and the push history table (the only SQL of the package)."""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from ..config import Config
from ..timeutil import iso_utc, now_utc
from .model import Workout, file_name, parse_markdown, render_markdown

STATUS_LINE = re.compile(r"^status:.*$", re.M)


def workouts_dir(config: Config) -> Path:
    return config.data_dir / "workouts"


def output_dir(config: Config) -> Path:
    """Where dry runs put the FIT files."""
    return workouts_dir(config) / "out"


def load(path: Path) -> Workout:
    try:
        text = path.read_text("utf-8")
    except OSError as exc:
        return Workout(name=path.stem, path=str(path), errors=[f"cannot read: {exc}"])
    workout = parse_markdown(text, path=str(path))
    if not workout.name:
        workout.name = path.stem  # keep the row readable; the missing name stays an error
    return workout


def list_workouts(config: Config, *, start: date | None = None, end: date | None = None) -> list[Workout]:
    """Every ``*.md`` in the workouts folder (parsed, invalid ones included), sorted by date then name."""
    folder = workouts_dir(config)
    if not folder.is_dir():
        return []
    items = [load(p) for p in sorted(folder.glob("*.md"))]
    if start is not None:
        items = [w for w in items if w.day is None or w.day >= start]
    if end is not None:
        items = [w for w in items if w.day is None or w.day <= end]
    items.sort(key=lambda w: (w.day or date.min, w.name))
    return items


def save(config: Config, workout: Workout, *, overwrite: bool = False) -> Path:
    """Write the Markdown file; refuses to clobber an existing one unless ``overwrite``."""
    folder = workouts_dir(config)
    folder.mkdir(parents=True, exist_ok=True)
    path = Path(workout.path) if workout.path else folder / file_name(workout)
    if path.exists() and not overwrite:
        raise FileExistsError(path)
    path.write_text(render_markdown(workout), "utf-8")
    workout.path = str(path)
    return path


def set_status(path: Path, status: str) -> None:
    """Change only the ``status:`` line of a file (the user's text and comments stay untouched)."""
    text = path.read_text("utf-8")
    if STATUS_LINE.search(text):
        text = STATUS_LINE.sub(f"status: {status}", text, count=1)
    elif text.startswith("---\n"):
        text = text.replace("---\n", f"---\nstatus: {status}\n", 1)
    else:
        text = f"---\nstatus: {status}\n---\n" + text
    path.write_text(text, "utf-8")


def existing_for_day(config: Config, day: date) -> list[Path]:
    folder = workouts_dir(config)
    return sorted(folder.glob(f"{day.isoformat()}-*.md")) if folder.is_dir() else []


# ---- push history ------------------------------------------------------------------------


def record_push(conn: sqlite3.Connection, workout: Workout, *, sha256: str, time_created_utc: str, target: str, watch_serial: str | None, dry_run: bool) -> int:
    cur = conn.execute(
        "INSERT INTO workout_pushes (path, name, local_date, sha256, time_created_utc, target, watch_serial, status, pushed_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (workout.path, workout.name, workout.day.isoformat() if workout.day else None, sha256, time_created_utc, target, watch_serial, "dry_run" if dry_run else "pushed", iso_utc(now_utc())),
    )
    conn.commit()
    return int(cur.lastrowid)


def pushes(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM workout_pushes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def pending_pushes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Pushed but not yet seen on the watch."""
    return [dict(r) for r in conn.execute("SELECT * FROM workout_pushes WHERE status = 'pushed' ORDER BY id").fetchall()]


def mark_verified(conn: sqlite3.Connection, push_id: int) -> None:
    conn.execute("UPDATE workout_pushes SET status = 'verified', verified_at_utc = ? WHERE id = ?", (iso_utc(now_utc()), push_id))
    conn.commit()


def push_status(conn: sqlite3.Connection, path: str) -> str | None:
    """``pushed`` or ``verified`` from the history (dry runs do not count), else None."""
    row = conn.execute("SELECT status FROM workout_pushes WHERE path = ? AND status != 'dry_run' ORDER BY id DESC LIMIT 1", (path,)).fetchone()
    return None if row is None else str(row[0])
