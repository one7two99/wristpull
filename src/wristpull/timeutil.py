"""Time helpers: FIT epoch conversion, ISO-8601 UTC strings, local dates."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=UTC)


def fit_to_utc(seconds: int) -> datetime:
    """Convert FIT seconds since 1989-12-31T00:00:00Z to an aware UTC datetime."""
    return FIT_EPOCH + timedelta(seconds=int(seconds))


def utc_to_fit(dt: datetime) -> int:
    return int((ensure_utc(dt) - FIT_EPOCH).total_seconds())


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso_utc(dt: datetime) -> str:
    """Render as ISO-8601 UTC with a Z suffix and second precision."""
    return ensure_utc(dt).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(text: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))


def now_utc() -> datetime:
    return datetime.now(UTC)


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone {name!r}") from exc


def local_date(dt: datetime, tz: ZoneInfo, *, shift_seconds: int = 0) -> str:
    """Local calendar date (YYYY-MM-DD) of a UTC instant.

    ``shift_seconds`` moves the instant before converting; the Garmin importer
    uses -1 so that a cumulative daily counter stamped exactly at local midnight
    is attributed to the day that just ended.
    """
    shifted = ensure_utc(dt) + timedelta(seconds=shift_seconds)
    return shifted.astimezone(tz).strftime("%Y-%m-%d")


def detect_local_zone() -> str:
    """Best-effort IANA zone name of this machine, falling back to UTC."""
    env = os.environ.get("TZ")
    if env and not env.startswith(":") and "/" in env:
        return env
    try:
        target = Path("/etc/localtime").resolve()
    except OSError:
        return "UTC"
    parts = target.parts
    if "zoneinfo" in parts:
        idx = parts.index("zoneinfo")
        name = "/".join(parts[idx + 1 :])
        if name:
            return name
    try:
        text = Path("/etc/timezone").read_text("utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    return "UTC"
