from pathlib import Path

from wristpull.importers.garmin import import_garmin
from wristpull.status import collect_status, last_imports, render_status


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


def test_last_imports_per_source(conn, config, garmin_tree: Path):
    assert last_imports(conn) == {}
    import_garmin(conn, config, path=garmin_tree)
    stamps = last_imports(conn)
    assert list(stamps) == ["garmin_usb"] and stamps["garmin_usb"].endswith("Z")


def test_status_over_mtp(conn, config, garmin_tree: Path):
    from fake_mtp import FakeMtp
    from wristpull.mtp import probe_mtp

    status = collect_status(conn, config, roots=lambda: [], mtp_urls=lambda: [], access="mtp", mtp_probe=lambda: probe_mtp(FakeMtp(garmin_tree.parent)))
    assert status["garmin_connected"] and status["garmin_root"] == "mtp://GARMIN" and status["garmin_access"] == "mtp"
    assert status["garmin_device"] == {"model": "fenix 6 Pro", "usb_serial": "0000deadbeef", "version": "2802"} and status["garmin_unmounted"] == []
    assert "connected over MTP (fenix 6 Pro, no mount needed)" in render_status(status)
    status = collect_status(conn, config, access="mtp", mtp_probe=lambda: probe_mtp(FakeMtp(garmin_tree.parent, present=False)))
    assert not status["garmin_connected"] and "no MTP device" in status["garmin_message"]
    assert "not connected (no MTP device" in render_status(status)
    status = collect_status(conn, config, access="mtp", mtp_probe=lambda: probe_mtp(FakeMtp(garmin_tree.parent, busy=True)))
    assert "holds the watch" in render_status(status)
