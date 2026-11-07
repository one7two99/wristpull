"""Build a synthetic Garmin Connect account export (portal zip layout) for tests. Never real data."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from conftest import build_activity_fit, build_hrv_fit, build_metrics_fit, build_monitor_fit, build_sleep_fit

ACCOUNT = "alice@example.com"
DI = "DI_CONNECT"


def export_fits(day: datetime | None = None) -> list[tuple[str, bytes]]:
    """Member names as Connect writes them (``<e-mail>_<id>.fit``) with one file of every kind."""
    day = day or datetime(2026, 10, 5, 22, 0, tzinfo=UTC)
    return [
        (f"{ACCOUNT}_7102332115.fit", build_monitor_fit(day)),
        (f"{ACCOUNT}_7102332116.fit", build_sleep_fit(day - timedelta(minutes=30), file_type=49)),
        (f"{ACCOUNT}_7102332117.fit", build_activity_fit(day + timedelta(hours=8))),
        (f"{ACCOUNT}_7102332118.fit", build_metrics_fit(day + timedelta(hours=9))),
        (f"{ACCOUNT}_7102332119.fit", build_hrv_fit(day + timedelta(hours=7))),
    ]


def uploads_zip(fits: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in fits:
            z.writestr(name, data)
    return buf.getvalue()


def build_export_zip(path: Path, *, fits: list[tuple[str, bytes]] | None = None, parts: int = 1, json_files: dict[str, object] | None = None, junk: bytes = b"not a fit file") -> Path:
    """Write a portal-style zip: DI_CONNECT/<folders>/..., the uploads split over ``parts`` inner zips."""
    fits = export_fits() if fits is None else fits
    chunks = [fits[i::parts] for i in range(parts)]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for i, chunk in enumerate(chunks, start=1):
            z.writestr(f"{DI}/DI-Connect-Uploaded-Files/UploadedFiles_0-_Part{i}.zip", uploads_zip(chunk + ([(f"{ACCOUNT}_broken.fit", junk)] if i == 1 and junk else [])))
        z.writestr(f"{DI}/DI-Connect-User/{ACCOUNT}-profile-image-large.png", b"\x89PNG")
        z.writestr("customer_data/customer.json", json.dumps({"id": 1}))
        for name, obj in (json_files or {}).items():
            z.writestr(f"{DI}/{name}", json.dumps(obj))
    return path


def build_export_dir(path: Path, *, fits: list[tuple[str, bytes]] | None = None, loose: bool = False) -> Path:
    """An extracted export: the uploads zip inside the folder, or (``loose``) the FIT files themselves."""
    fits = export_fits() if fits is None else fits
    uploads = path / DI / "DI-Connect-Uploaded-Files"
    uploads.mkdir(parents=True)
    if loose:
        for name, data in fits:
            (uploads / name).write_bytes(data)
    else:
        (uploads / "UploadedFiles_0-_Part1.zip").write_bytes(uploads_zip(fits))
    return path
