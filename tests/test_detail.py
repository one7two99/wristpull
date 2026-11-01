from pathlib import Path
from zoneinfo import ZoneInfo

from gb_db import build_gb_db
from wristpull.detail import activities, activity_detail, downsample, local_clock, nights, sleep_night, sports
from wristpull.importers.garmin import import_garmin
from wristpull.importers.phone import import_gadgetbridge_db

TZ = ZoneInfo("Europe/Berlin")


def test_activity_detail_and_lists(conn, config, garmin_tree: Path, tmp_path: Path):
    import_garmin(conn, config, path=garmin_tree)
    import_gadgetbridge_db(conn, config, build_gb_db(tmp_path / "Gadgetbridge.db"))
    rows = activities(conn, "2026-10-01", "2026-11-30")
    assert [r["source"] for r in rows] == ["gadgetbridge_zepp", "garmin_usb"]  # newest first
    assert activities(conn, "2026-10-01", "2026-11-30", sport="run")[0]["source"] == "garmin_usb"
    assert activities(conn, "2026-10-01", "2026-11-30", sport="evening")[0]["profile_name"] == "Evening walk"
    assert sports(conn) == ["running", "walking"]
    garmin = next(r for r in rows if r["source"] == "garmin_usb")
    detail = activity_detail(conn, garmin["id"])
    assert detail["sport"] == "running" and len(detail["laps"]) == 3 and len(detail["records"]) == 360
    assert detail["records"][0]["hr"] == 120 and detail["archive_path"].startswith("garmin_usb/")
    assert len(detail["hr_zones_s"]) >= 1
    assert activity_detail(conn, 9999) is None


def test_downsample():
    assert downsample([1, 2, 3, 4], 2) == [1.5, 3.5]
    assert downsample([1, None, None, 4], 2) == [1.0, 4.0]
    assert downsample([None, None], 1) == [None]
    assert downsample([1, 2], 5) == [1, 2]


def test_sleep_night_and_nights(conn, config, garmin_tree: Path):
    import_garmin(conn, config, path=garmin_tree)
    rows = nights(conn, "2026-10-01", "2026-10-31")
    assert len(rows) == 1 and rows[0]["source"] == "garmin_usb"
    night = sleep_night(conn, rows[0]["id"])
    assert night["stages"][0]["stage"] == "unmeasurable" and len(night["stages"]) == 12
    assert night["hrv"]["n"] >= 0 and "summary" in night["hrv"]
    assert set(night["spo2"]) == {"n", "avg", "min"} and set(night["heart_rate"]) == {"n", "min", "avg"}
    assert sleep_night(conn, 4242) is None
    assert local_clock("2026-10-05T21:10:00Z", TZ) == "23:10"
