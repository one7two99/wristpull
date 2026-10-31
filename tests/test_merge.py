from wristpull.db import insert_rows
from wristpull.merge import merged_activities, merged_daily, merged_sleep, source_coverage


def _day(source, date, **values):
    row = {"source": source, "local_date": date, "tz": "Europe/Berlin", "updated_at_utc": "2026-10-31T00:00:00Z"}
    row.update(values)
    return row


def test_merged_daily_prefers_usb_and_fills_gaps(conn):
    insert_rows(conn, "daily_summary", [
        _day("garmin_usb", "2026-10-30", steps=8000, resting_hr=None, stress_avg=30.0),
        _day("gadgetbridge_garmin", "2026-10-30", steps=7990, resting_hr=52, stress_avg=31.0),
        _day("gadgetbridge_zepp", "2026-10-30", steps=7800, resting_hr=50, temperature_avg=33.1),
        _day("gadgetbridge_garmin", "2026-10-31", steps=100),
    ])
    days = merged_daily(conn, "2026-10-01", "2026-10-31")
    assert [d.date for d in days] == ["2026-10-30", "2026-10-31"]
    first = days[0]
    assert first.garmin["steps"] == 8000  # usb wins
    assert first.garmin["resting_hr"] == 52  # gap filled from gadgetbridge
    assert first.garmin["stress_avg"] == 30.0
    assert first.strap["steps"] == 7800 and first.strap["temperature_avg"] == 33.1
    assert first.sources == ["gadgetbridge_garmin", "gadgetbridge_zepp", "garmin_usb"]
    assert days[1].garmin["steps"] == 100 and days[1].strap is None
    assert source_coverage(conn, "2026-10-01", "2026-10-31") == {"gadgetbridge_garmin": 2, "gadgetbridge_zepp": 1, "garmin_usb": 1}


def test_merged_activities_and_sleep_dedup(conn):
    base = {"tz": "Europe/Berlin", "local_date": "2026-10-30", "sport": "running"}
    insert_rows(conn, "activities", [
        {"source": "garmin_usb", "start_utc": "2026-10-30T06:00:00Z", "distance_m": 5000, **base},
        {"source": "gadgetbridge_garmin", "start_utc": "2026-10-30T06:00:03Z", "distance_m": 4999, **base},
        {"source": "gadgetbridge_garmin", "start_utc": "2026-10-30T17:00:00Z", "distance_m": 3000, **base},
        {"source": "gadgetbridge_zepp", "start_utc": "2026-10-30T06:00:00Z", "distance_m": 5100, **{**base, "sport": "walking"}},
    ])
    acts = merged_activities(conn, "2026-10-30", "2026-10-30")
    assert [(a["device"], a["source"], a["distance_m"]) for a in acts] == [
        ("garmin", "garmin_usb", 5000.0),
        ("strap", "gadgetbridge_zepp", 5100.0),
        ("garmin", "gadgetbridge_garmin", 3000.0),
    ]
    insert_rows(conn, "sleep", [
        {"source": "garmin_usb", "start_utc": "2026-10-29T22:00:00Z", "end_utc": "2026-10-30T05:00:00Z", "tz": "Europe/Berlin", "local_date": "2026-10-30", "duration_s": 25000},
        {"source": "gadgetbridge_garmin", "start_utc": "2026-10-29T22:00:30Z", "end_utc": "2026-10-30T05:00:00Z", "tz": "Europe/Berlin", "local_date": "2026-10-30", "duration_s": 24000},
        {"source": "gadgetbridge_zepp", "start_utc": "2026-10-29T22:10:00Z", "end_utc": "2026-10-30T05:10:00Z", "tz": "Europe/Berlin", "local_date": "2026-10-30", "duration_s": 24500},
    ])
    nights = merged_sleep(conn, "2026-10-30", "2026-10-30")
    assert [(n["device"], n["duration_s"]) for n in nights] == [("garmin", 25000), ("strap", 24500)]
