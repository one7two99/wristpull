from pathlib import Path

from wristpull.importers.garmin import import_garmin
from wristpull.status import collect_status, render_status


def test_status_device_detection_is_injectable(conn, config, garmin_tree: Path):
    import_garmin(conn, config, path=garmin_tree)
    status = collect_status(conn, config, roots=lambda: [garmin_tree.parent], mtp_urls=lambda: ["mtp://unused/"])
    assert status["garmin_connected"] and status["garmin_root"] == str(garmin_tree)
    assert status["garmin_device"]["model"] == "fenix 6 Pro" and status["garmin_unmounted"] == []
    assert status["tables"]["heart_rate"]["count"] == 672 and status["last_import"]
    text = render_status(status)
    assert "connected (fenix 6 Pro)" in text and "garmin_usb" in text

    status = collect_status(conn, config, roots=lambda: [], mtp_urls=lambda: ["mtp://091e_4cdb_0000deadbeef/"])
    assert not status["garmin_connected"] and status["garmin_unmounted"] == ["mtp://091e_4cdb_0000deadbeef/"]
    assert "attached but not mounted" in render_status(status)

    status = collect_status(conn, config, roots=lambda: [], mtp_urls=lambda: [])
    assert "not connected" in render_status(status)
