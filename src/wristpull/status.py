"""Overview of the local data store and connected devices."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from .config import Config
from .db import scalar, table_count
from .garmin_usb import candidate_roots, find_garmin_root, gio_garmin_mtp_urls, read_device_info
from .mtp import MtpDeviceInfo, MtpTools, probe_mtp, resolve_access
from .timeutil import iso_utc
from .zeppos import SOURCE as ZEPP_SOURCE
from .importers.strap import auth_key_source
from .zeppos.store import all_sync

MtpProbe = Callable[[], tuple[MtpDeviceInfo | None, str]]

SAMPLE_TABLES = (
    "activities",
    "activity_records",
    "heart_rate",
    "steps",
    "stress",
    "body_battery",
    "sleep",
    "sleep_stages",
    "hrv",
    "hrv_values",
    "spo2",
    "respiration",
    "temperature",
    "resting_hr",
    "metrics",
    "daily_summary",
)


def _range(conn: sqlite3.Connection, table: str, column: str) -> tuple[str | None, str | None]:
    row = conn.execute(f"SELECT MIN({column}), MAX({column}) FROM {table}").fetchone()
    return (row[0], row[1]) if row else (None, None)


def last_imports(conn: sqlite3.Connection) -> dict[str, str]:
    """When each source (``garmin_usb``, ``gadgetbridge``, ...) last delivered a raw file (UTC ISO)."""
    rows = conn.execute("SELECT source, MAX(imported_at_utc) FROM raw_files GROUP BY source").fetchall()
    return {str(r[0]): str(r[1]) for r in rows if r[1]}


def collect_status(
    conn: sqlite3.Connection,
    config: Config,
    *,
    roots: Callable[[], list[Path]] | None = None,
    mtp_urls: Callable[[], list[str]] = gio_garmin_mtp_urls,
    access: str | None = None,
    mtp_probe: MtpProbe | None = None,
) -> dict[str, object]:
    """Counts per table and raw file plus the device state.

    ``roots`` lists the directories to search for a mounted watch (default: the system
    mount points) and ``mtp_urls`` the attached-but-unmounted MTP devices; tests inject both.
    With ``access == "mtp"`` (default: ``[garmin] access`` resolved) the watch is probed through
    ``mtp_probe`` (default: the MTP tools) instead; ``garmin_access`` and ``garmin_message`` say so.
    """
    raw_by = conn.execute(
        "SELECT source, kind, COUNT(*), SUM(parse_status = 'error') FROM raw_files GROUP BY source, kind ORDER BY source, kind"
    ).fetchall()
    tables = {}
    for table in SAMPLE_TABLES:
        count = table_count(conn, table)
        if table == "activities":
            rng = _range(conn, table, "start_utc")
        elif table in ("sleep",):
            rng = _range(conn, table, "end_utc")
        elif table in ("daily_summary",):
            rng = _range(conn, table, "local_date")
        elif table in ("activity_records", "sleep_stages"):
            rng = (None, None)
        else:
            rng = _range(conn, table, "ts_utc")
        tables[table] = {"count": count, "first": rng[0], "last": rng[1]}
    strap = {
        "address": config.strap_address or None,
        "key_source": auth_key_source(config.strap_authkey_path) if config.strap_address else None,
        "sync": {kind: iso_utc(when) for kind, when in all_sync(conn, ZEPP_SOURCE).items()},
    }
    access = access or resolve_access(config)
    garmin_root: Path | None = None
    device: dict[str, str] = {}
    unmounted: list[str] = []
    message: str | None = None
    root_label: str | None = None
    if access == "mtp":
        info, message = (mtp_probe or (lambda: probe_mtp(MtpTools())))()
        if info is not None:
            root_label = "mtp://GARMIN"
            device = {k: v for k, v in (("model", info.model), ("usb_serial", info.serial), ("version", info.version)) if v}
    else:
        search = roots or (lambda: candidate_roots(extra=config.garmin_mount_hints))
        garmin_root = find_garmin_root(search())
        if garmin_root is not None:
            root_label = str(garmin_root)
            device = read_device_info(garmin_root)
        else:
            unmounted = mtp_urls()
    return {
        "config_path": str(config.path) if config.path else None,
        "db_path": str(config.db_path),
        "db_size_bytes": config.db_path.stat().st_size if config.db_path.exists() else 0,
        "raw_dir": str(config.raw_dir),
        "timezone": config.timezone,
        "raw_files": [{"source": r[0], "kind": r[1], "count": r[2], "errors": r[3] or 0} for r in raw_by],
        "last_import": scalar(conn, "SELECT MAX(imported_at_utc) FROM raw_files"),
        "tables": tables,
        "garmin_connected": root_label is not None,
        "garmin_root": root_label,
        "garmin_device": device,
        "garmin_unmounted": unmounted,
        "garmin_access": access,
        "garmin_message": message,
        "strap": strap,
    }


def render_status(status: dict[str, object]) -> str:
    lines = ["wristpull status", ""]
    lines.append(f"config:     {status['config_path'] or '(defaults, no file)'}")
    lines.append(f"database:   {status['db_path']} ({int(status['db_size_bytes']) // 1024} KiB)")
    lines.append(f"raw files:  {status['raw_dir']}")
    lines.append(f"timezone:   {status['timezone']}")
    lines.append(f"last import: {status['last_import'] or 'never'}")
    lines.append("")
    if status["garmin_connected"] and status.get("garmin_access") == "mtp":
        dev = status["garmin_device"] or {}
        lines.append(f"Garmin:     connected over MTP ({dev.get('model', 'Garmin device')}, no mount needed)")
    elif status["garmin_connected"]:
        dev = status["garmin_device"] or {}
        label = dev.get("model", "Garmin device")
        lines.append(f"Garmin:     connected ({label}) at {status['garmin_root']}")
    elif status.get("garmin_access") == "mtp":
        lines.append(f"Garmin:     not connected ({status.get('garmin_message') or 'no MTP device'})")
    elif status.get("garmin_unmounted"):
        lines.append(f"Garmin:     attached but not mounted ({', '.join(status['garmin_unmounted'])}); 'wristpull import garmin' will try to mount it")  # type: ignore[arg-type]
    else:
        lines.append("Garmin:     not connected (no GARMIN folder found under /run/media, /media or gvfs MTP mounts)")
    strap = status.get("strap") or {}
    if not strap.get("address"):
        lines.append("Strap:      not configured (set [strap] address; `wristpull strap scan` lists bands in range)")
    else:
        source = strap.get("key_source")
        key = {"env": "key from the environment", "file": "key from the file"}.get(source or "", "NO auth key (set WRISTPULL_STRAP_AUTHKEY)")
        sync = strap.get("sync") or {}
        last = max(sync.values()) if sync else None
        lines.append(f"Strap:      {strap['address']} over Bluetooth, {key}, last pull {last or 'never'} (checked when pulling)")
    lines.append("")
    lines.append("raw files by source/kind:")
    raw_files = status["raw_files"]
    if not raw_files:
        lines.append("  (none)")
    for entry in raw_files:  # type: ignore[union-attr]
        err = f", {entry['errors']} with errors" if entry["errors"] else ""
        lines.append(f"  {entry['source']:<20} {entry['kind']:<10} {entry['count']:>6}{err}")
    lines.append("")
    lines.append(f"{'table':<18}{'rows':>9}  {'first':<21}{'last'}")
    for name, info in status["tables"].items():  # type: ignore[union-attr]
        first = info["first"] or ""
        last = info["last"] or ""
        lines.append(f"{name:<18}{info['count']:>9}  {first:<21}{last}")
    return "\n".join(lines)


def data_dir_exists(config: Config) -> bool:
    return Path(config.data_dir).exists()
