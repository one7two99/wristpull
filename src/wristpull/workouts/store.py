"""Workout files under <data_dir>/workouts/ and the push history table (the only SQL of the package)."""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from ..config import Config
from ..timeutil import iso_utc, now_utc
from .model import Workout, file_name, mark_name_clashes, parse_markdown, render_markdown

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
    """Every ``*.md`` in the workouts folder (parsed, invalid ones included), sorted by date then name.

    Two files of the same week with the same name are both flagged invalid (``mark_name_clashes``)."""
    folder = workouts_dir(config)
    if not folder.is_dir():
        return []
    items = [load(p) for p in sorted(folder.glob("*.md"))]
    mark_name_clashes(items)
    if start is not None:
        items = [w for w in items if w.day is None or w.day >= start]
    if end is not None:
        items = [w for w in items if w.day is None or w.day <= end]
    items.sort(key=lambda w: (w.day or date.min, w.name))
    return items


def mark_folder_clashes(config: Config, workouts: list[Workout]) -> None:
    """``mark_name_clashes`` for ``workouts`` against each other and every other file in the workouts folder."""
    mine = {str(Path(w.path).resolve()) for w in workouts if w.path}
    others = [w for w in list_workouts(config) if not w.path or str(Path(w.path).resolve()) not in mine]
    mark_name_clashes(workouts + others)


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


FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.md$")
MAX_FILE_BYTES = 64 * 1024


def workout_file(config: Config, name: str) -> Path:
    """The path of a workout file given by its bare name; anything that could leave the folder is refused."""
    if not FILE_NAME.match(name or "") or ".." in name:
        raise ValueError(f"not a workout file name: {name!r}")
    folder = workouts_dir(config).resolve()
    path = (folder / name).resolve()
    if path.parent != folder:
        raise ValueError(f"not a workout file name: {name!r}")
    return path


def check_text(config: Config, name: str | None, text: str) -> Workout:
    """Parse edited Markdown as if it were saved under ``name`` (for a live check), name clashes included."""
    path = workout_file(config, name) if name else None
    workout = parse_markdown(text, path=str(path) if path else None)
    mark_folder_clashes(config, [workout])
    return workout


def write_text(config: Config, name: str, text: str) -> Workout:
    """Replace an existing workout file with edited Markdown (kept verbatim) and return it parsed.

    Only files that already exist in the workouts folder can be written; new ones come from ``save``.
    Invalid Markdown is still written (the user may save work in progress); the errors come back.
    """
    path = workout_file(config, name)
    if not path.is_file():
        raise FileNotFoundError(path)
    data = text.replace("\r\n", "\n")
    if len(data.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError("workout file too large")
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(data if data.endswith("\n") else data + "\n", "utf-8")
    tmp.replace(path)
    return check_text(config, name, data)


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


def push_rows_for(conn: sqlite3.Connection, *, sha256: str | None = None, name: str | None = None, time_created_utc: str | None = None) -> list[dict[str, Any]]:
    """History rows matching a file on the watch: by content hash, or by (name, time_created)."""
    rows = conn.execute(
        "SELECT * FROM workout_pushes WHERE status != 'dry_run' AND (sha256 = ? OR (name = ? AND time_created_utc = ?)) ORDER BY id",
        (sha256 or "", name or "", time_created_utc or ""),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_removed(conn: sqlite3.Connection, ids: list[int]) -> None:
    """The watch no longer holds these pushes (the user removed the file)."""
    conn.executemany("UPDATE workout_pushes SET status = 'removed' WHERE id = ?", [(i,) for i in ids])
    conn.commit()


def upcoming(config: Config, conn: sqlite3.Connection, today: date, *, force: bool = False) -> list[Workout]:
    """Valid dated workouts from ``today`` on; without ``force`` only those not currently on the watch
    (never pushed, or removed again)."""
    selected: list[Workout] = []
    for workout in list_workouts(config, start=today):
        if not workout.valid or workout.day is None:
            continue
        if not force and (push_status(conn, workout.path or "") or "") in ("pushed", "verified"):
            continue
        selected.append(workout)
    return selected
