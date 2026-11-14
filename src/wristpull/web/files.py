"""The Files page: a safe view of the data directory (listing, download, preview), no SQL here.

Every path from the browser is relative to the data directory and resolved before use; anything
that leaves the directory, is hidden, lies under ``adb/`` (the private adb key), ``staging/``
(half-copied files) or ``backups/`` (archives with secrets: the Backup page serves them), or is the
secrets store itself is refused.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HIDDEN_DIRS = frozenset({"adb", "staging", "backups"})
HIDDEN_FILES = frozenset({"secrets.json", "secrets.json.tmp"})  # at the top of the data directory
TEXT_SUFFIXES = frozenset({".md", ".txt", ".csv", ".toml", ".json", ".log", ".ics"})
PREVIEW_LINES = 40
PREVIEW_BYTES = 64 * 1024


class BadPath(ValueError):
    """The browser asked for something outside the visible part of the data directory."""


def safe_path(data_dir: Path, rel: str | None) -> Path:
    """``data_dir / rel`` after resolving, or :class:`BadPath` when it leaves the directory or is hidden."""
    root = data_dir.resolve()
    text = (rel or "").strip().strip("/")
    if "\0" in text:
        raise BadPath(text)
    target = (root / text).resolve() if text else root
    try:
        parts = target.relative_to(root).parts
    except ValueError as exc:
        raise BadPath(text) from exc
    if parts and (parts[0] in HIDDEN_DIRS or any(p.startswith(".") for p in parts)):
        raise BadPath(text)
    if len(parts) == 1 and parts[0] in HIDDEN_FILES:
        raise BadPath(text)
    return target


def relative(data_dir: Path, path: Path) -> str:
    return path.resolve().relative_to(data_dir.resolve()).as_posix()


def file_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".fit":
        return "fit"
    if suffix in TEXT_SUFFIXES:
        return "text"
    if suffix == ".svg":
        return "image"
    return "other"


def listing(data_dir: Path, rel: str | None) -> dict[str, Any]:
    """Folders and files directly under ``rel`` (no recursion), hidden entries left out, folders first."""
    folder = safe_path(data_dir, rel)
    if not folder.is_dir():
        raise BadPath(rel or "")
    here = relative(data_dir, folder)
    crumbs = []
    acc = ""
    for part in Path(here).parts:
        acc = f"{acc}/{part}" if acc else part
        crumbs.append({"name": part, "rel": acc})
    dirs: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    for child in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if child.name.startswith(".") or (folder == data_dir.resolve() and child.name in HIDDEN_DIRS | HIDDEN_FILES):
            continue
        try:
            stat = child.stat()
        except OSError:
            continue
        entry = {"name": child.name, "rel": relative(data_dir, child), "mtime": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat().replace("+00:00", "Z")}
        if child.is_dir():
            dirs.append(entry)
        else:
            files.append({**entry, "size": stat.st_size, "kind": file_kind(child)})
    return {"rel": here, "crumbs": crumbs, "parent": crumbs[-2]["rel"] if len(crumbs) > 1 else ("" if crumbs else None), "dirs": dirs, "files": files}


def preview(path: Path, *, lines: int = PREVIEW_LINES) -> list[str]:
    """The first lines of a text file (at most ``PREVIEW_BYTES`` read)."""
    with path.open("rb") as handle:
        text = handle.read(PREVIEW_BYTES).decode("utf-8", errors="replace")
    return text.splitlines()[:lines]


def file_info(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {"name": path.name, "size": stat.st_size, "mtime": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat().replace("+00:00", "Z"), "kind": file_kind(path)}
