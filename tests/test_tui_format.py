from datetime import date

import pytest

from wristpull.tui import i18n
from wristpull.tui.format import fmt_bytes, fmt_clock, fmt_date, fmt_datetime, fmt_delta, fmt_duration, fmt_float, fmt_int, fmt_km, sparkline


@pytest.fixture(autouse=True)
def _english():
    i18n.set_language("en")
    yield
    i18n.set_language("en")


def test_numbers_by_language():
    assert fmt_int(6842) == "6,842" and fmt_int(6842, "de") == "6 842"
    assert fmt_int(None) == "–"
    assert fmt_float(7.86, 1) == "7.9" and fmt_float(1234.5, 1, "de") == "1 234,5"
    assert fmt_bytes(8_200_000) == "7.8 MiB" and fmt_bytes(512) == "512 B" and fmt_bytes(8_200_000, "de") == "7,8 MiB"
    assert fmt_km(8210) == "8.2 km" and fmt_km(None) == "–"


def test_durations_and_dates():
    assert fmt_duration(7 * 3600 + 12 * 60) == "7:12" and fmt_duration(None) == "–"
    assert fmt_clock(45 * 60 + 12) == "45:12" and fmt_clock(6730) == "1:52:10"
    assert fmt_date("2026-10-31") == "Oct 31" and fmt_date("2026-10-31", "de") == "31.10."
    assert fmt_date(date(2026, 10, 31), year=True) == "2026-10-31" and fmt_date("2026-10-31", "de", year=True) == "31.10.2026"
    assert fmt_datetime("2026-10-31T15:02:00Z") == "Oct 31 15:02" and fmt_datetime(None) == "–"


def test_sparkline_and_delta():
    assert sparkline([1, 2, 3, None, 8]) == "▁▂▃ █"
    assert sparkline([None, None]) == "  " and sparkline([5, 5]) == "▄▄"
    assert fmt_delta(52, 53) == "▼ −1" and fmt_delta(6842, 6100) == "▲ +742"
    assert fmt_delta(None, 1) == "–" and fmt_delta(10, 10) == "= 0"
    assert fmt_delta(7.5, 7.0, digits=1, suffix=" h") == "▲ +0.5 h"
