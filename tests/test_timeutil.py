from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from wristpull.timeutil import fit_to_utc, iso_utc, local_date, parse_iso_utc, utc_to_fit, zone


def test_fit_epoch_roundtrip():
    dt = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    assert fit_to_utc(utc_to_fit(dt)) == dt
    assert fit_to_utc(0) == datetime(1989, 12, 31, tzinfo=UTC)


def test_iso_utc_formats_with_z():
    assert iso_utc(datetime(2026, 10, 31, 6, 12, 33, 500000, tzinfo=UTC)) == "2026-10-31T06:12:33Z"
    assert parse_iso_utc("2026-10-31T06:12:33Z") == datetime(2026, 10, 31, 6, 12, 33, tzinfo=UTC)


def test_local_date_and_midnight_shift():
    tz = ZoneInfo("Europe/Berlin")
    midnight_local = datetime(2026, 10, 5, 22, 0, tzinfo=UTC)  # 2026-10-06 00:00 CEST
    assert local_date(midnight_local, tz) == "2026-10-06"
    assert local_date(midnight_local, tz, shift_seconds=-1) == "2026-10-05"


def test_zone_rejects_unknown():
    assert zone("Europe/Berlin").key == "Europe/Berlin"
    with pytest.raises(ValueError):
        zone("Mars/Olympus")
