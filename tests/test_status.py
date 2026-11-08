from pathlib import Path

from fake_mtp import FakeMtp
from wristpull.importers.garmin import import_garmin
from wristpull.mtp import probe_mtp
from wristpull.status import collect_status, last_imports, render_status


def test_status_probes_the_watch_over_mtp(conn, config, garmin_tree: Path):
    import_garmin(conn, config, path=garmin_tree)
    status = collect_status(conn, config, mtp_probe=lambda: probe_mtp(FakeMtp(garmin_tree.parent)))
    assert status["garmin_connected"] and status["garmin_root"] == "mtp://GARMIN"
    assert status["garmin_device"] == {"model": "fenix 6 Pro", "usb_serial": "0000deadbeef", "version": "2802"}
    assert status["tables"]["heart_rate"]["count"] == 672 and status["last_import"]
    text = render_status(status)
    assert "connected over USB (MTP): fenix 6 Pro" in text and "garmin_usb" in text

    status = collect_status(conn, config, mtp_probe=lambda: probe_mtp(FakeMtp(garmin_tree.parent, present=False)))
    assert not status["garmin_connected"] and "no MTP device" in status["garmin_message"]
    assert "not connected (no MTP device" in render_status(status)
    status = collect_status(conn, config, mtp_probe=lambda: probe_mtp(FakeMtp(garmin_tree.parent, busy=True)))
    assert "holds the watch" in render_status(status)


def test_last_imports_per_source(conn, config, garmin_tree: Path):
    assert last_imports(conn) == {}
    import_garmin(conn, config, path=garmin_tree)
    stamps = last_imports(conn)
    assert list(stamps) == ["garmin_usb"] and stamps["garmin_usb"].endswith("Z")
