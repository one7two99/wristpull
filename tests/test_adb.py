"""adb helpers that do not need a phone: serial shapes, the sysfs scan, connect parsing."""

from pathlib import Path

import pytest

from wristpull.adb import Adb, NoDeviceError, adb_usb_devices, is_wireless_serial


def test_is_wireless_serial():
    assert is_wireless_serial("192.168.1.20:37099")
    assert is_wireless_serial("phone.lan:5555")
    assert not is_wireless_serial("LD20241010171")
    assert not is_wireless_serial("emulator-5554")
    assert not is_wireless_serial(None)


def _usb_device(sysfs: Path, name: str, vendor: str, product: str, iface: tuple[str, str, str] | None) -> None:
    dev = sysfs / name
    dev.mkdir(parents=True)
    (dev / "idVendor").write_text(vendor + "\n")
    (dev / "idProduct").write_text(product + "\n")
    if iface:
        i = sysfs / f"{name}:1.0"
        i.mkdir()
        for key, value in zip(("bInterfaceClass", "bInterfaceSubClass", "bInterfaceProtocol"), iface):
            (i / key).write_text(value + "\n")


def test_adb_usb_devices_scans_sysfs(tmp_path: Path):
    sysfs = tmp_path / "usb"
    _usb_device(sysfs, "1-2", "18d1", "4ee7", ("ff", "42", "01"))  # phone with ADB interface
    _usb_device(sysfs, "1-3", "091e", "4cdb", ("ff", "ff", "00"))  # the watch (MTP, vendor specific)
    _usb_device(sysfs, "1-4", "046d", "c52b", None)  # a mouse without interface files
    (sysfs / "usb1").mkdir()  # a root hub without idVendor
    assert adb_usb_devices(sysfs) == ["18d1:4ee7"]
    assert adb_usb_devices(tmp_path / "missing") == []


def test_connect_decides_by_text(monkeypatch):
    import subprocess

    class Proc:
        def __init__(self, out: str) -> None:
            self.returncode, self.stdout, self.stderr = 0, out, ""

    answers = iter(["connected to 10.0.0.5:5555\n", "failed to connect to '10.0.0.5:5555': Connection refused\n"])
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: (calls.append(argv), Proc(next(answers)))[1])
    adb = Adb(binary="adb")
    assert adb.connect("10.0.0.5:5555") == "connected to 10.0.0.5:5555"
    assert calls[0] == ["adb", "connect", "10.0.0.5:5555"]
    with pytest.raises(NoDeviceError, match="failed to connect"):
        adb.connect("10.0.0.5:5555")
