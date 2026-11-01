import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import build_monitor_fit
from fake_adb import FakeAdb
from gb_db import build_gb_db
from wristpull.adb import Device, NoDeviceError, parse_devices
from wristpull.db import table_count
from wristpull.importers.phone import GB_FILES_DIR, import_gadgetbridge_fit_dir, kind_from_path, pick_device, pull_phone, trigger_export

GARMIN_DIR = f"{GB_FILES_DIR}/AA:BB:CC:DD:EE:01"


@pytest.fixture
def phone(tmp_path: Path) -> FakeAdb:
    root = tmp_path / "phone"
    (root / "sdcard/Gadgetbridge").mkdir(parents=True)
    build_gb_db(root / "sdcard/Gadgetbridge/Gadgetbridge.db")
    mon = root / GARMIN_DIR.lstrip("/") / "MONITOR/2026"
    mon.mkdir(parents=True)
    (mon / "MONITOR_2026-10-31_16-58-52_129.fit").write_bytes(build_monitor_fit(datetime(2026, 10, 30, 23, 0, tzinfo=UTC)))
    seg = root / GARMIN_DIR.lstrip("/") / "SEGMENT_LIST/2026"
    seg.mkdir(parents=True)
    (seg / "SEGMENT_LIST_1.fit").write_bytes(b"\x0e\x10garbage")
    return FakeAdb(root)


def test_parse_devices_and_pick():
    text = "List of devices attached\nFAKE0001\tdevice\nemulator-5554\toffline\n\n"
    assert parse_devices(text) == [Device("FAKE0001", "device"), Device("emulator-5554", "offline")]
    adb = FakeAdb(Path("/nonexistent"), devices=parse_devices(text))
    assert pick_device(adb, None) == "FAKE0001"
    assert pick_device(adb, "FAKE0001") == "FAKE0001"
    with pytest.raises(NoDeviceError):
        pick_device(adb, "other")
    with pytest.raises(NoDeviceError):
        pick_device(FakeAdb(Path("/x"), devices=[]), None)


def test_kind_from_path():
    assert kind_from_path("F0:99/MONITOR/2026/x.fit") == "monitor"
    assert kind_from_path("F0:99/HRVSTATUS/2026/x.fit") == "hrv"
    assert kind_from_path("F0:99/SEGMENT_LIST/2026/x.fit") == "other"


def test_trigger_export_waits_for_mtime_change(phone: FakeAdb):
    phone.export_on_trigger = True
    logs: list[str] = []
    ok = trigger_export(phone, "/sdcard/Gadgetbridge/Gadgetbridge.db", wait=10, sync=True, log=logs.append, sleep=lambda _s: None)
    assert ok and phone.broadcasts == [
        "nodomain.freeyourgadget.gadgetbridge.command.ACTIVITY_SYNC",
        "nodomain.freeyourgadget.gadgetbridge.command.TRIGGER_DATABASE_EXPORT",
    ]
    phone.export_on_trigger = False
    clock = iter([0, 5, 20, 30])
    ok = trigger_export(phone, "/sdcard/Gadgetbridge/Gadgetbridge.db", wait=10, sync=False, log=logs.append, sleep=lambda _s: None, clock=lambda: next(clock))
    assert not ok and "did not change" in logs[-1]


def test_pull_phone_end_to_end(conn, config, phone: FakeAdb):
    config.phone_serial = "FAKE0001"
    phone.export_on_trigger = True
    logs: list[str] = []
    stats = pull_phone(conn, config, adb=phone, wait=5, log=logs.append, sleep=lambda _s: None)
    assert stats.device == "FAKE0001"
    assert stats.db_pulled and stats.db_new
    assert stats.fit_found == 2 and stats.fit_new == 2 and stats.fit_skipped == 0
    assert stats.errors == 1  # the garbage SEGMENT_LIST file
    assert stats.rows["heart_rate"] == 295 + 672
    assert any("Amazfit Helio Strap" in d and "gadgetbridge_zepp" in d for d in stats.db_devices)
    assert table_count(conn, "raw_files", "source = 'gadgetbridge'") == 1
    assert table_count(conn, "raw_files", "source = 'gadgetbridge_garmin'") == 2
    assert table_count(conn, "raw_files", "parse_status = 'error'") == 1
    assert table_count(conn, "heart_rate", "source = 'gadgetbridge_zepp'") == 295
    assert table_count(conn, "heart_rate", "source = 'gadgetbridge_garmin'") == 672
    assert table_count(conn, "sleep", "source = 'gadgetbridge_zepp'") == 1
    assert table_count(conn, "temperature") == 60
    day = conn.execute("SELECT * FROM daily_summary WHERE source = 'gadgetbridge_zepp' AND local_date = '2026-11-01'").fetchone()
    assert day["sleep_duration_s"] == 90 * 60 and day["resting_hr"] == 51
    garmin_day = conn.execute("SELECT steps FROM daily_summary WHERE source = 'gadgetbridge_garmin' AND local_date = '2026-10-31'").fetchone()
    assert garmin_day["steps"] == 52 * 120 + 2000

    # second pull: nothing new, nothing pulled again
    phone.pulled.clear()
    again = pull_phone(conn, config, adb=phone, trigger=False, log=logs.append, sleep=lambda _s: None)
    assert again.db_pulled and not again.db_new
    assert again.fit_new == 0 and again.fit_skipped == 2
    assert [r for r, _ in phone.pulled] == ["/sdcard/Gadgetbridge/Gadgetbridge.db"]
    assert table_count(conn, "heart_rate") == 295 + 672


def test_pull_phone_without_export(conn, config, phone: FakeAdb):
    (phone.root / "sdcard/Gadgetbridge/Gadgetbridge.db").unlink()
    stats = pull_phone(conn, config, adb=phone, trigger=False, sleep=lambda _s: None)
    assert not stats.db_pulled and any("not found" in m for m in stats.messages)
    assert stats.fit_new == 2


def test_pull_phone_dry_run(conn, config, phone: FakeAdb):
    stats = pull_phone(conn, config, adb=phone, dry_run=True, sleep=lambda _s: None)
    assert stats.fit_new == 2 and phone.pulled == [] and phone.broadcasts == []
    assert table_count(conn, "raw_files") == 0


def test_import_gadgetbridge_fit_dir_local_copy(conn, config, phone: FakeAdb, tmp_path: Path):
    local = tmp_path / "copy"
    shutil.copytree(phone.root / GB_FILES_DIR.lstrip("/"), local)
    found, new, skipped, errors, counts = import_gadgetbridge_fit_dir(conn, config, local)
    assert (found, new, skipped, errors) == (2, 2, 0, 1)
    assert counts["heart_rate"] == 672


def test_pull_phone_stops_on_request(conn, config, phone: FakeAdb):
    logs: list[str] = []
    stats = pull_phone(conn, config, adb=phone, trigger=True, sync=False, wait=30, log=logs.append, sleep=lambda _s: None, stop=lambda: True)
    assert stats.stopped and not stats.db_pulled and phone.pulled == []
    assert any("stopped on request" in line for line in logs)
    # without a trigger the database is pulled first, the FIT loop honours the stop before the first file
    stats = pull_phone(conn, config, adb=phone, trigger=False, log=logs.append, stop=lambda: True)
    assert stats.db_pulled and stats.stopped and stats.fit_new == 0 and len(phone.pulled) == 1
