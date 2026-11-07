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


# --- JSON files (phase 2) -----------------------------------------------------------------------


def uds_record(date: str, *, steps: int | None = 6000, resting_hr: int | None = 55, stress: int | None = 30, spo2: float | None = 96.0) -> dict:
    """One daily summary as the Aggregator UDS files carry it (fields that wristpull reads, plus noise)."""
    rec: dict = {
        "userProfilePK": 1, "calendarDate": date, "uuid": "x", "durationInMilliseconds": 86400000,
        "totalSteps": steps, "totalDistanceMeters": steps * 0.7 if steps is not None else None,
        "activeKilocalories": 420.0, "activeSeconds": 3000, "highlyActiveSeconds": 600,
        "moderateIntensityMinutes": 20, "vigorousIntensityMinutes": 5,
        "minHeartRate": 48, "maxHeartRate": 150, "restingHeartRate": resting_hr,
        "includesWellnessData": True, "averageSpo2Value": spo2, "lowestSpo2Value": 91,
        "respiration": {"avgWakingRespirationValue": 14.5, "highestRespirationValue": 20.0},
        "bodyBattery": {"bodyBatteryStatList": [
            {"bodyBatteryStatType": "HIGHEST", "statsValue": 88, "statTimestamp": f"{date}T07:00:00.0"},
            {"bodyBatteryStatType": "LOWEST", "statsValue": 23, "statTimestamp": f"{date}T22:00:00.0"},
        ]},
    }
    if stress is not None:
        rec["allDayStress"] = {"aggregatorList": [
            {"type": "AWAKE", "averageStressLevel": stress + 10, "maxStressLevel": 80},
            {"type": "TOTAL", "averageStressLevel": stress, "maxStressLevel": 77},
        ]}
    return rec


def junk_uds_records() -> list[dict]:
    """The placeholder rows real exports carry: FIT epoch and empty days."""
    return [
        {"userProfilePK": 1, "calendarDate": "1989-12-31", "totalSteps": None, "includesWellnessData": False},
        {"userProfilePK": 1, "calendarDate": "2006-01-01", "totalSteps": None, "includesWellnessData": None},
    ]


def sleep_record(date: str, *, start: str, end: str, scores: bool = True, stages: bool = True) -> dict:
    rec: dict = {"sleepStartTimestampGMT": start, "sleepEndTimestampGMT": end, "calendarDate": date, "sleepWindowConfirmationType": "ENHANCED_CONFIRMED_FINAL", "retro": False}
    if stages:
        rec.update({"deepSleepSeconds": 3660, "lightSleepSeconds": 20460, "remSleepSeconds": 7080, "awakeSleepSeconds": 360, "unmeasurableSeconds": 0, "awakeCount": 2, "avgSleepStress": 13.85, "averageRespiration": 14.3})
    if scores:
        rec["sleepScores"] = {"overallScore": 81, "qualityScore": 77, "durationScore": 90, "recoveryScore": 70, "deepScore": 60}
    return rec
