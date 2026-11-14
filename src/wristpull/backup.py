"""Backup and restore of the whole wristpull state: data directory, config file, ``.env`` and certificates.

One tar.gz holds everything a new machine needs (``docs/setup.md`` step 10): the data directory
(raw files, database, adb keys, secrets store, coach profile, workouts, reports), the config file, the
``.env`` next to ``compose.yaml`` (UID/GID, the two paths, dashboard secrets, API keys, the band's auth
key) and the own-CA certificate files from ``deploy/certs/``. The archive therefore contains secrets:
it is written with mode 0600 and belongs on a disk you trust, never in mail or chat.

Layout inside the archive (everything below ``wristpull-backup/``, format 2)::

    MANIFEST.json   format, when, wristpull and schema version, host paths, counts (first member)
    env             the .env file (absent when the backup was made without one)
    config.toml     the config file (absent when there was none)
    certs/...       the files of deploy/certs/ (format 2; absent when the folder is empty or unknown)
    data/...        the data directory without ``staging/`` and ``backups/`` and without the database's
                    -wal/-shm files; ``wristpull.sqlite`` is a consistent copy taken with SQLite's backup
                    API, so the dashboard may keep running while the backup is made
    SHA256SUMS      one line per file above, checked on restore (last member)

Restore rewrites only what is specific to the machine: ``UID``/``GID`` and the two paths in
``.env``, ``[storage] data_dir`` in the config. Secrets, timezone, strap address and everything else
are kept as they were. Nothing is deleted on restore; with ``force`` files from the archive replace
existing ones. ``.env`` and the config are written in place (both are single-file bind mounts in the
containers), and an existing database is not replaced but overwritten through SQLite's backup API, so
the dashboard and the agent, which hold connections to it, keep working on the same file.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import tarfile
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .config import config_file_present
from .runtime import host_path
from .timeutil import iso_utc

FORMAT = 2  # 2 (2026-11): certs/ members; format 1 archives are still read
SUPPORTED_FORMATS = (1, 2)
ROOT = "wristpull-backup"
MANIFEST_NAME = "MANIFEST.json"
SUMS_NAME = "SHA256SUMS"
ENV_NAME = "env"
CONFIG_NAME = "config.toml"
CERTS_NAME = "certs"
DATA_NAME = "data"
DB_NAME = "wristpull.sqlite"
DB_SIDE_FILES = frozenset({DB_NAME + "-wal", DB_NAME + "-shm", DB_NAME + "-journal"})
BACKUPS_DIR_NAME = "backups"  # archives made from the dashboard or the CLI live here, inside the data directory
EXCLUDE_TOP_DIRS = frozenset({"staging", BACKUPS_DIR_NAME})  # downloads in flight, and the archives themselves
ENV_DATA_KEY, ENV_CONFIG_KEY = "WRISTPULL_DATA", "WRISTPULL_CONFIG"
ARCHIVE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.tar\.gz$")
PART_SUFFIX = ".part"
Log = Callable[[str], None] | None


class BackupError(RuntimeError):
    pass


@dataclass
class Manifest:
    format: int = FORMAT
    created_at: str = ""
    wristpull_version: str = __version__
    schema_version: str | None = None
    data_dir: str | None = None  # as the host sees it (from .env when present)
    config_file: str | None = None
    env_file: str | None = None
    files: int = 0
    bytes: int = 0
    has_env: bool = False
    has_config: bool = False
    has_certs: bool = False
    certs: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Manifest:
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise BackupError(f"unreadable manifest: {exc}") from exc
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        manifest = cls(**known)
        if manifest.format not in SUPPORTED_FORMATS:
            raise BackupError(f"unsupported backup format {manifest.format} (this wristpull reads {', '.join(map(str, SUPPORTED_FORMATS))})")
        return manifest


@dataclass
class BackupStats:
    path: Path
    manifest: Manifest


@dataclass
class RestoreStats:
    data_dir: Path
    config_path: Path | None
    env_path: Path | None
    manifest: Manifest
    certs_dir: Path | None = None
    files: int = 0
    db_replaced_live: bool = False  # the existing database was overwritten in place (connections stay valid)
    notes: list[str] = field(default_factory=list)


@dataclass
class ArchiveInfo:
    """One archive in the backups folder: its manifest, or why it could not be read."""

    path: Path
    name: str
    size: int
    mtime: str
    manifest: Manifest | None = None
    error: str | None = None


def default_name(now: datetime | None = None) -> str:
    """``wristpull-backup-<date and time>.tar.gz``: the name reads like the clock on the wall. An aware ``now``
    keeps its zone (callers pass the configured one: the container's clock is UTC), a naive or missing one
    means the process's local time."""
    if now is None or now.tzinfo is None:
        now = (now or datetime.now(UTC)).astimezone()
    return f"wristpull-backup-{now:%Y-%m-%d-%H%M}.tar.gz"


def backups_dir(data_dir: Path) -> Path:
    """Where archives live by default: ``<data_dir>/backups/`` (excluded from the archives themselves)."""
    return Path(data_dir) / BACKUPS_DIR_NAME


def archive_name_ok(name: str) -> bool:
    """A bare archive file name (no separators) ending in ``.tar.gz``."""
    return bool(ARCHIVE_NAME.match(name)) and "/" not in name and "\\" not in name


def list_backups(folder: Path) -> list[ArchiveInfo]:
    """The archives in a folder, newest first; unreadable ones carry an error instead of a manifest."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    out: list[ArchiveInfo] = []
    for path in folder.iterdir():
        if not path.is_file() or not archive_name_ok(path.name):
            continue
        st = path.stat()
        info = ArchiveInfo(path=path, name=path.name, size=st.st_size, mtime=iso_utc(datetime.fromtimestamp(st.st_mtime, UTC)))
        try:
            info.manifest = read_manifest(path)
        except BackupError as exc:
            info.error = str(exc)
        out.append(info)
    out.sort(key=lambda i: (i.mtime, i.name), reverse=True)
    return out


def delete_backup(folder: Path, name: str) -> Path:
    """Remove one archive from the folder (the only deletion this module does, on explicit request)."""
    if not archive_name_ok(name):
        raise BackupError(f"not an archive name: {name!r}")
    path = Path(folder) / name
    if not path.is_file():
        raise BackupError(f"no such archive: {name}")
    path.unlink()
    return path


def upload_target(folder: Path, name: str | None, now: datetime | None = None) -> Path:
    """Where an uploaded archive goes: its own name when it is a plain archive name that is still free,
    else ``wristpull-backup-upload-<stamp>.tar.gz``."""
    folder = Path(folder)
    if name and archive_name_ok(name) and not (folder / name).exists() and not (folder / (name + PART_SUFFIX)).exists():
        return folder / name
    stamp = (now or datetime.now(UTC)).astimezone()
    base = f"wristpull-backup-upload-{stamp:%Y-%m-%d-%H%M%S}"
    candidate = folder / f"{base}.tar.gz"
    n = 1
    while candidate.exists() or (folder / (candidate.name + PART_SUFFIX)).exists():
        n += 1
        candidate = folder / f"{base}-{n}.tar.gz"
    return candidate


def needed_free_bytes(manifest: Manifest) -> int:
    """Free space a restore wants: the unpacked size plus a tenth (temporary database copy, WAL)."""
    return int(manifest.bytes * 1.1)


def data_present(data_dir: Path) -> list[str]:
    """What an existing data directory already holds, as short lines (empty = a fresh instance): row counts
    of the main tables and the non-empty top-level folders. ``backups/``, ``staging/``, the secrets store
    and an empty database do not count."""
    data_dir = Path(data_dir)
    lines: list[str] = []
    db = data_dir / DB_NAME
    if db.is_file():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.DatabaseError:
            conn = None
        if conn is not None:
            try:
                for table, label in (("raw_files", "raw files"), ("activities", "activities"), ("daily_summary", "days"), ("coach_runs", "coach runs")):
                    try:
                        n = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608 (fixed names)
                    except sqlite3.DatabaseError:
                        continue
                    if n:
                        lines.append(f"{n} {label}")
            finally:
                conn.close()
    if data_dir.is_dir():
        folders = sorted(p.name for p in data_dir.iterdir() if p.is_dir() and p.name not in EXCLUDE_TOP_DIRS and not p.is_symlink() and any(p.iterdir()))
        if folders:
            lines.append("folders " + ", ".join(f"{f}/" for f in folders))
    return lines


# --- .env and config rewriting (pure) ------------------------------------------------------------

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def parse_env(text: str) -> dict[str, str]:
    """``KEY=VALUE`` lines of a dotenv file; comments and blanks skipped, simple quotes stripped."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        m = _ENV_LINE.match(line)
        if not m or line.lstrip().startswith("#"):
            continue
        value = m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[m.group(1)] = value
    return values


def rewrite_env(text: str, updates: Mapping[str, str]) -> str:
    """Replace the value of every key in ``updates`` (comments and order kept); missing keys are appended."""
    pending = dict(updates)
    out: list[str] = []
    for line in text.splitlines():
        m = _ENV_LINE.match(line)
        if m and not line.lstrip().startswith("#") and m.group(1) in pending:
            out.append(f"{m.group(1)}={pending.pop(m.group(1))}")
        else:
            out.append(line)
    if pending:
        out.append("# set by wristpull backup restore")
        out.extend(f"{k}={v}" for k, v in pending.items())
    return "\n".join(out) + "\n"


def rewrite_data_dir(text: str, data_dir: Path) -> str:
    """Set ``[storage] data_dir`` in a config file's text, keeping everything else verbatim."""
    value = f'data_dir = "{data_dir}"'
    out: list[str] = []
    section = None
    done = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if section == "storage" and not done:
                out.append(value)
                done = True
            section = stripped[1:-1].strip()
        elif section == "storage" and not done and re.match(r"^\s*data_dir\s*=", line):
            out.append(value)
            done = True
            continue
        out.append(line)
    if not done:
        if section == "storage":
            out.append(value)
        else:
            out[:0] = ["[storage]", value, ""]
    return "\n".join(out) + "\n"


# --- create -------------------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _anonymous(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _data_entries(data_dir: Path) -> list[tuple[Path, str]]:
    """(path, archive name) of every directory and file under the data directory, sorted, without
    the excluded top-level folders, the database and its side files (the database is added from its copy)."""
    entries: list[tuple[Path, str]] = []
    for root, dirs, files in os.walk(data_dir):
        rel = Path(root).relative_to(data_dir)
        if rel == Path("."):
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_TOP_DIRS)
        else:
            dirs.sort()
            entries.append((Path(root), f"{ROOT}/{DATA_NAME}/{rel.as_posix()}"))
        for name in sorted(files):
            if rel == Path(".") and (name == DB_NAME or name in DB_SIDE_FILES):
                continue
            path = Path(root) / name
            if path.is_symlink() or not path.is_file():
                continue
            entries.append((path, f"{ROOT}/{DATA_NAME}/{(rel / name).as_posix()}"))
    return entries


def _cert_entries(certs_dir: Path | None) -> list[Path]:
    """The regular, non-hidden files directly in the certificates folder (``.gitkeep`` and the like stay out)."""
    if not certs_dir or not Path(certs_dir).is_dir():
        return []
    return sorted(p for p in Path(certs_dir).iterdir() if p.is_file() and not p.is_symlink() and not p.name.startswith("."))


def _copy_database(src: Path, dst: Path) -> str | None:
    """A consistent copy via SQLite's online backup; returns the schema version stored in it. With an
    existing ``dst`` its content is overwritten page by page (the file stays, open connections survive)."""
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(dst), timeout=30)
        try:
            source.backup(target)  # in one step: a stepped copy restarts whenever another connection writes the source (the agent's heartbeat)
            try:
                row = target.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            except sqlite3.DatabaseError:
                row = None
        finally:
            target.close()
    finally:
        source.close()
    return str(row[0]) if row else None


def _add_text(tar: tarfile.TarFile, name: str, data: bytes, mode: int = 0o600) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.mtime = int(datetime.now(UTC).timestamp())
    tar.addfile(_anonymous(info), io.BytesIO(data))


def create_backup(
    data_dir: Path,
    config_path: Path | None,
    env_path: Path | None,
    out: Path,
    *,
    certs_dir: Path | None = None,
    log: Log = None,
    now: datetime | None = None,
) -> BackupStats:
    """Write the archive; ``out`` is a file name (``*.tar.gz``) or a directory (then ``default_name``).
    ``certs_dir`` is ``deploy/certs/`` (own-CA certificate and key); its files go in as ``certs/<name>``."""
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        raise BackupError(f"data directory not found: {data_dir}")
    out = Path(out)
    if out.is_dir() or not out.name.endswith(".tar.gz"):
        out = out / default_name(now)
    if out.exists():
        raise BackupError(f"refusing to overwrite {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    config_path = Path(config_path) if config_path and config_file_present(Path(config_path)) else None
    env_path = Path(env_path) if env_path and Path(env_path).is_file() else None
    env_values = parse_env(env_path.read_text("utf-8")) if env_path else {}
    certs = _cert_entries(certs_dir)

    manifest = Manifest(
        created_at=iso_utc(now or datetime.now(UTC)),
        data_dir=env_values.get(ENV_DATA_KEY) or str(host_path(data_dir)),
        config_file=env_values.get(ENV_CONFIG_KEY) or (str(config_path) if config_path else None),
        env_file=str(env_path) if env_path else None,
        has_env=env_path is not None,
        has_config=config_path is not None,
        has_certs=bool(certs),
        certs=[p.name for p in certs],
    )
    sums: list[str] = []
    part = out.with_name(out.name + PART_SUFFIX)
    try:
        with tempfile.TemporaryDirectory(prefix="wristpull-backup-") as tmp:
            db_copy: Path | None = None
            if (data_dir / DB_NAME).is_file():
                db_copy = Path(tmp) / DB_NAME
                manifest.schema_version = _copy_database(data_dir / DB_NAME, db_copy)
                if log:
                    log(f"database copied (schema version {manifest.schema_version or 'unknown'})")
            entries = _data_entries(data_dir)
            files = [(p, n) for p, n in entries if p.is_file()]
            if db_copy:
                files.append((db_copy, f"{ROOT}/{DATA_NAME}/{DB_NAME}"))
            manifest.files = len(files) + int(manifest.has_env) + int(manifest.has_config) + len(certs)
            manifest.bytes = sum(p.stat().st_size for p, _ in files)
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh, tarfile.open(fileobj=fh, mode="w:gz", compresslevel=6) as tar:  # FIT files hardly compress better at 9, and 9 is slow
                root = tarfile.TarInfo(ROOT)
                root.type = tarfile.DIRTYPE
                root.mode = 0o700
                tar.addfile(_anonymous(root))
                _add_text(tar, f"{ROOT}/{MANIFEST_NAME}", manifest.to_json().encode("utf-8"))
                for label, path in ((ENV_NAME, env_path), (CONFIG_NAME, config_path)):
                    if path:
                        tar.add(path, arcname=f"{ROOT}/{label}", recursive=False, filter=_anonymous)
                        sums.append(f"{_sha256(path)}  {label}")
                if certs:
                    folder = tarfile.TarInfo(f"{ROOT}/{CERTS_NAME}")
                    folder.type = tarfile.DIRTYPE
                    folder.mode = 0o700
                    tar.addfile(_anonymous(folder))
                    for path in certs:
                        tar.add(path, arcname=f"{ROOT}/{CERTS_NAME}/{path.name}", recursive=False, filter=_anonymous)
                        sums.append(f"{_sha256(path)}  {CERTS_NAME}/{path.name}")
                for path, name in entries:
                    if path.is_dir():
                        tar.add(path, arcname=name, recursive=False, filter=_anonymous)
                for path, name in files:
                    tar.add(path, arcname=name, recursive=False, filter=_anonymous)
                    sums.append(f"{_sha256(path)}  {name[len(ROOT) + 1:]}")
                _add_text(tar, f"{ROOT}/{SUMS_NAME}", ("\n".join(sums) + "\n").encode("utf-8"))
        os.replace(part, out)
    finally:
        part.unlink(missing_ok=True)
    if log:
        log(f"{manifest.files} files, {manifest.bytes / 1e6:.1f} MB → {out}")
    return BackupStats(path=out, manifest=manifest)


# --- show / restore -----------------------------------------------------------------------------


def _member_path(member: tarfile.TarInfo) -> str:
    """The name below the archive root; refuses anything that is not under it."""
    name = member.name.rstrip("/")
    if name == ROOT:
        return ""
    if not name.startswith(ROOT + "/"):
        raise BackupError(f"not a wristpull backup: unexpected entry {member.name!r}")
    rel = name[len(ROOT) + 1 :]
    if rel.startswith("/") or ".." in rel.split("/"):
        raise BackupError(f"refusing unsafe entry {member.name!r}")
    return rel


def read_manifest(archive: Path) -> Manifest:
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                if _member_path(member) == MANIFEST_NAME:
                    fh = tar.extractfile(member)
                    return Manifest.from_json(fh.read().decode("utf-8")) if fh else Manifest()
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BackupError(f"cannot read {archive}: {exc}") from exc
    raise BackupError(f"not a wristpull backup (no {MANIFEST_NAME}): {archive}")


def _parse_sums(text: str) -> dict[str, str]:
    sums: dict[str, str] = {}
    for line in text.splitlines():
        if line.strip():
            digest, _, name = line.partition("  ")
            sums[name] = digest
    return sums


def _read_sums(archive: Path) -> dict[str, str]:
    """The checksum list alone (the last member): a pass over the archive without extracting anything."""
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                if _member_path(member) == SUMS_NAME:
                    fh = tar.extractfile(member)
                    return _parse_sums(fh.read().decode("utf-8")) if fh else {}
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BackupError(f"cannot read {archive}: {exc}") from exc
    raise BackupError(f"archive has no {SUMS_NAME}: incomplete backup")


def _occupied(data_dir: Path, config_path: Path | None, env_path: Path | None, manifest: Manifest) -> list[str]:
    found: list[str] = []
    if data_dir.is_dir() and any(p.name not in EXCLUDE_TOP_DIRS for p in data_dir.iterdir()):
        found.append(f"data directory {data_dir} is not empty")
    if manifest.has_config and config_path and config_file_present(config_path):
        found.append(f"config file {config_path} exists")
    if manifest.has_env and env_path and env_path.is_file() and env_path.stat().st_size > 0:
        found.append(f"{env_path} exists")
    return found


def restore_backup(
    archive: Path,
    *,
    data_dir: Path,
    config_path: Path | None,
    env_path: Path | None,
    certs_dir: Path | None = None,
    host_data_dir: Path | None = None,
    host_config: Path | None = None,
    uid: int | None = None,
    gid: int | None = None,
    force: bool = False,
    log: Log = None,
) -> RestoreStats:
    """Unpack the archive. ``host_data_dir``/``host_config`` are the paths as the host sees them (they
    go into ``.env`` and the config); inside the container they differ from ``data_dir``/``config_path``.
    ``certs_dir`` receives the ``certs/`` members (``deploy/certs/``). An existing database is overwritten
    through SQLite's backup API after its checksum was verified, so open connections keep working."""
    archive, data_dir = Path(archive), Path(data_dir)
    manifest = read_manifest(archive)
    config_path = Path(config_path) if config_path and manifest.has_config else None
    env_path = Path(env_path) if env_path and manifest.has_env else None
    certs_dir = Path(certs_dir) if certs_dir and manifest.has_certs else None
    occupied = _occupied(data_dir, config_path, env_path, manifest)
    if occupied and not force:
        raise BackupError("; ".join(occupied) + " (use --force to let the archive overwrite files; nothing is deleted)")
    host_data_dir = Path(host_data_dir) if host_data_dir else host_path(data_dir)
    host_config = Path(host_config) if host_config else config_path
    stats = RestoreStats(data_dir=data_dir, config_path=config_path, env_path=env_path, manifest=manifest, certs_dir=certs_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    live_db = data_dir / DB_NAME
    sums: dict[str, str] = _read_sums(archive) if live_db.is_file() else {}  # the live copy wants the checksum first
    seen: dict[str, str] = {}
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                rel = _member_path(member)
                if not rel or rel == MANIFEST_NAME:
                    continue
                if rel == SUMS_NAME:
                    fh = tar.extractfile(member)
                    sums = _parse_sums(fh.read().decode("utf-8")) if fh else {}
                    continue
                if rel in (ENV_NAME, CONFIG_NAME):
                    fh = tar.extractfile(member)
                    raw = fh.read() if fh else b""
                    seen[rel] = hashlib.sha256(raw).hexdigest()
                    if rel == ENV_NAME and env_path:
                        _write_env(env_path, raw, host_data_dir, host_config, uid, gid)
                        stats.files += 1
                        if log:
                            log(f".env → {env_path}")
                    elif rel == CONFIG_NAME and config_path:
                        config_path.parent.mkdir(parents=True, exist_ok=True)
                        _write_text_in_place(config_path, rewrite_data_dir(raw.decode("utf-8"), host_data_dir).encode("utf-8"), mode=None)
                        stats.files += 1
                        if log:
                            log(f"config → {config_path}")
                    continue
                if rel == CERTS_NAME or rel.startswith(CERTS_NAME + "/"):
                    name = rel[len(CERTS_NAME) + 1 :]
                    if not name:
                        continue
                    if "/" in name or not member.isfile():
                        raise BackupError(f"refusing unsafe entry {member.name!r}")
                    fh = tar.extractfile(member)
                    raw = fh.read() if fh else b""
                    seen[rel] = hashlib.sha256(raw).hexdigest()
                    if certs_dir:
                        certs_dir.mkdir(parents=True, exist_ok=True)
                        _write_text_in_place(certs_dir / name, raw, mode=member.mode & 0o7777 or 0o600)
                        stats.files += 1
                        if log:
                            log(f"certificate file → {certs_dir / name}")
                    continue
                if not rel.startswith(DATA_NAME + "/"):
                    raise BackupError(f"not a wristpull backup: unexpected entry {member.name!r}")
                target_rel = rel[len(DATA_NAME) + 1 :]
                if not target_rel:
                    continue
                if not (member.isdir() or member.isfile()):
                    stats.notes.append(f"skipped {rel}: not a regular file")
                    continue
                target = data_dir / target_rel
                if member.isfile() and target_rel == DB_NAME and target.is_file():
                    seen[rel] = _restore_live_database(tar, member, data_dir, sums.get(rel), log)
                    stats.files += 1
                    stats.db_replaced_live = True
                    continue
                if member.isfile() and target.exists():
                    target.unlink()  # read-only raw files cannot be opened for writing
                member.name = target_rel
                tar.extract(member, data_dir, filter="data")
                if member.isfile():
                    target.chmod(member.mode & 0o7555 | (member.mode & 0o200))  # the data filter adds owner write: raw files stay read-only
                    stats.files += 1
                    seen[rel] = _sha256(target)
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BackupError(f"cannot unpack {archive}: {exc}") from exc
    _verify(sums, seen)
    if log:
        log(f"{stats.files} files restored into {data_dir}")
    return stats


def _restore_live_database(tar: tarfile.TarFile, member: tarfile.TarInfo, data_dir: Path, expected: str | None, log: Log) -> str:
    """Unpack the archive's database next to the live one, check its digest, then copy it INTO the live
    file with the backup API (same inode: the dashboard's and the agent's connections stay valid) and
    checkpoint the WAL. Returns the digest of the unpacked copy."""
    tmp = data_dir / f".{DB_NAME}.restore-{os.getpid()}"
    tmp.unlink(missing_ok=True)
    member.name = tmp.name
    try:
        tar.extract(member, data_dir, filter="data")
        digest = _sha256(tmp)
        if expected is not None and digest != expected:
            raise BackupError(f"checksum mismatch: {DATA_NAME}/{DB_NAME} (the live database was left untouched)")
        _copy_database(tmp, data_dir / DB_NAME)
        conn = sqlite3.connect(str(data_dir / DB_NAME), timeout=30)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    finally:
        for side in ("", "-wal", "-shm", "-journal"):
            tmp.with_name(tmp.name + side).unlink(missing_ok=True)
    if log:
        log("database overwritten in place (open connections keep working)")
    return digest


def _host_id(name: str) -> int:
    """``os.getuid``/``os.getgid``, or 1000 where they do not exist (Windows)."""
    getter = getattr(os, name, None)
    return int(getter()) if getter else 1000


def _write_text_in_place(path: Path, data: bytes, *, mode: int | None = 0o600) -> None:
    """Write a file without replacing it: an existing file is truncated and rewritten (single-file bind
    mounts such as ``.env`` and the config inside the containers cannot be unlinked), a new one is created
    with ``mode`` (``None``: the default for new files)."""
    path = Path(path)
    if path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
        if mode is not None:
            try:
                os.fchmod(fd, mode)
            except OSError:
                pass  # a bind mount may refuse; the mode is then the one the file already has
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666 if mode is None else mode)  # None: the umask decides
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def _write_env(path: Path, raw: bytes, host_data_dir: Path, host_config: Path | None, uid: int | None, gid: int | None) -> None:
    updates = {
        "UID": str(_host_id("getuid") if uid is None else uid),
        "GID": str(_host_id("getgid") if gid is None else gid),
        ENV_DATA_KEY: str(host_data_dir),
    }
    if host_config:
        updates[ENV_CONFIG_KEY] = str(host_config)
    text = rewrite_env(raw.decode("utf-8"), updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_text_in_place(path, text.encode("utf-8"), mode=0o600)


def _verify(expected: Mapping[str, str], seen: Mapping[str, str]) -> None:
    if not expected:
        raise BackupError(f"archive has no {SUMS_NAME}: incomplete backup")
    bad = sorted(name for name, digest in expected.items() if name in seen and seen[name] != digest)
    missing = sorted(name for name in expected if name not in seen)
    if bad:
        raise BackupError("checksum mismatch: " + ", ".join(bad[:5]) + (" …" if len(bad) > 5 else ""))
    if missing:
        raise BackupError("files listed but not in the archive: " + ", ".join(missing[:5]))


def describe(manifest: Manifest) -> Iterable[str]:
    yield f"created:   {manifest.created_at or 'unknown'} (wristpull {manifest.wristpull_version}, schema {manifest.schema_version or 'unknown'})"
    yield f"data dir:  {manifest.data_dir or 'unknown'} ({manifest.files} files, {manifest.bytes / 1e6:.1f} MB)"
    yield f"config:    {(manifest.config_file or 'included') if manifest.has_config else 'not included'}"
    yield f"env:       {'included (secrets!)' if manifest.has_env else 'not included'}"
    yield f"certs:     {', '.join(manifest.certs) if manifest.has_certs else 'not included'}"
