"""Backup and restore of the whole wristpull state: data directory, config file and ``.env``.

One tar.gz holds everything a new machine needs (``docs/setup.md`` step 10): the data directory
(raw files, database, adb keys, coach profile, workouts, reports), the config file and the ``.env``
next to ``compose.yaml`` (UID/GID, the two paths, dashboard secrets, API keys, the band's auth key).
The archive therefore contains secrets: it is written with mode 0600 and belongs on a disk you
trust, never in mail or chat.

Layout inside the archive (everything below ``wristpull-backup/``)::

    MANIFEST.json   format, when, wristpull and schema version, host paths, counts (first member)
    env             the .env file (absent when the backup was made without one)
    config.toml     the config file (absent when there was none)
    data/...        the data directory without ``staging/`` and without the database's -wal/-shm
                    files; ``wristpull.sqlite`` is a consistent copy taken with SQLite's backup API,
                    so the dashboard may keep running while the backup is made
    SHA256SUMS      one line per file above, checked on restore (last member)

Restore rewrites only what is specific to the machine: ``UID``/``GID`` and the two paths in
``.env``, ``[storage] data_dir`` in the config. Secrets, timezone, strap address and everything else
are kept as they were. Nothing is deleted on restore; with ``force`` files from the archive replace
existing ones.
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

FORMAT = 1
ROOT = "wristpull-backup"
MANIFEST_NAME = "MANIFEST.json"
SUMS_NAME = "SHA256SUMS"
ENV_NAME = "env"
CONFIG_NAME = "config.toml"
DATA_NAME = "data"
DB_NAME = "wristpull.sqlite"
DB_SIDE_FILES = frozenset({DB_NAME + "-wal", DB_NAME + "-shm", DB_NAME + "-journal"})
EXCLUDE_TOP_DIRS = frozenset({"staging"})  # downloads in flight, never worth keeping
ENV_DATA_KEY, ENV_CONFIG_KEY = "WRISTPULL_DATA", "WRISTPULL_CONFIG"
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
        if manifest.format != FORMAT:
            raise BackupError(f"unsupported backup format {manifest.format} (this wristpull reads {FORMAT})")
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
    files: int = 0
    notes: list[str] = field(default_factory=list)


def default_name(now: datetime | None = None) -> str:
    """``wristpull-backup-<local date and time>.tar.gz``: the name reads like the clock on the wall."""
    now = (now or datetime.now(UTC)).astimezone()
    return f"wristpull-backup-{now:%Y-%m-%d-%H%M}.tar.gz"


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


def _copy_database(src: Path, dst: Path) -> str | None:
    """A consistent copy via SQLite's online backup; returns the schema version stored in it."""
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target)
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
    log: Log = None,
    now: datetime | None = None,
) -> BackupStats:
    """Write the archive; ``out`` is a file name (``*.tar.gz``) or a directory (then ``default_name``)."""
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

    manifest = Manifest(
        created_at=iso_utc(now or datetime.now(UTC)),
        data_dir=env_values.get(ENV_DATA_KEY) or str(host_path(data_dir)),
        config_file=env_values.get(ENV_CONFIG_KEY) or (str(config_path) if config_path else None),
        env_file=str(env_path) if env_path else None,
        has_env=env_path is not None,
        has_config=config_path is not None,
    )
    sums: list[str] = []
    part = out.with_name(out.name + ".part")
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
            manifest.files = len(files) + int(manifest.has_env) + int(manifest.has_config)
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


def _occupied(data_dir: Path, config_path: Path | None, env_path: Path | None, manifest: Manifest) -> list[str]:
    found: list[str] = []
    if data_dir.is_dir() and any(data_dir.iterdir()):
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
    host_data_dir: Path | None = None,
    host_config: Path | None = None,
    uid: int | None = None,
    gid: int | None = None,
    force: bool = False,
    log: Log = None,
) -> RestoreStats:
    """Unpack the archive. ``host_data_dir``/``host_config`` are the paths as the host sees them (they
    go into ``.env`` and the config); inside the container they differ from ``data_dir``/``config_path``."""
    archive, data_dir = Path(archive), Path(data_dir)
    manifest = read_manifest(archive)
    config_path = Path(config_path) if config_path and manifest.has_config else None
    env_path = Path(env_path) if env_path and manifest.has_env else None
    occupied = _occupied(data_dir, config_path, env_path, manifest)
    if occupied and not force:
        raise BackupError("; ".join(occupied) + " (use --force to let the archive overwrite files; nothing is deleted)")
    host_data_dir = Path(host_data_dir) if host_data_dir else host_path(data_dir)
    host_config = Path(host_config) if host_config else config_path
    stats = RestoreStats(data_dir=data_dir, config_path=config_path, env_path=env_path, manifest=manifest)
    data_dir.mkdir(parents=True, exist_ok=True)
    sums: dict[str, str] = {}
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
                        config_path.write_text(rewrite_data_dir(raw.decode("utf-8"), host_data_dir), "utf-8")
                        stats.files += 1
                        if log:
                            log(f"config → {config_path}")
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


def _write_env(path: Path, raw: bytes, host_data_dir: Path, host_config: Path | None, uid: int | None, gid: int | None) -> None:
    updates = {
        "UID": str(os.getuid() if uid is None else uid),
        "GID": str(os.getgid() if gid is None else gid),
        ENV_DATA_KEY: str(host_data_dir),
    }
    if host_config:
        updates[ENV_CONFIG_KEY] = str(host_config)
    text = rewrite_env(raw.decode("utf-8"), updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


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
