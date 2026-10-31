from pathlib import Path

from wristpull.garmin_usb import KDE_HINT, gio_garmin_mtp_urls, try_mount_garmin
from wristpull.importers.garmin import import_garmin, locate_garmin

GIO_LIST = """Drive(0): INTEL SSDPEKKF256G8L
  Type: GProxyDrive (GProxyVolumeMonitorUDisks2)
Volume(0): fenix 6X Pro
  Type: GProxyVolume (GProxyVolumeMonitorMTP)
  activation_root=mtp://091e_4cdb_0000deadbeef/
Volume(1): Pixel
  Type: GProxyVolume (GProxyVolumeMonitorMTP)
  activation_root=mtp://18d1_4ee1_ABC/
"""


class FakeRunner:
    def __init__(self, mount_rc: int = 0, mount_err: str = "", kiod: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.mount_rc = mount_rc
        self.mount_err = mount_err
        self.kiod = kiod

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        if argv[:3] == ["gio", "mount", "-li"]:
            return 0, GIO_LIST, ""
        if argv[:2] == ["gio", "mount"]:
            return self.mount_rc, "", self.mount_err
        if argv[:2] == ["pgrep", "-x"]:
            return (0 if self.kiod else 1), "", ""
        raise AssertionError(argv)


def test_gio_urls_filters_garmin_vendor():
    runner = FakeRunner()
    assert gio_garmin_mtp_urls(runner) == ["mtp://091e_4cdb_0000deadbeef/"]
    assert gio_garmin_mtp_urls(lambda argv: (1, "", "no gio")) == []


def test_try_mount_success_and_kde_hint():
    ok = FakeRunner()
    assert try_mount_garmin(ok) == (True, "mounted mtp://091e_4cdb_0000deadbeef/")
    assert ok.calls[-1] == ["gio", "mount", "mtp://091e_4cdb_0000deadbeef/"]

    blocked = FakeRunner(mount_rc=2, mount_err="gio: mtp://091e_4cdb_0000deadbeef/: Unable to open MTP device “001,039”", kiod=True)
    mounted, message = try_mount_garmin(blocked)
    assert not mounted
    assert "kquitapp6 kiod6 && gio mount mtp://091e_4cdb_0000deadbeef/" in message
    assert KDE_HINT.splitlines()[0] in message

    other = FakeRunner(mount_rc=2, mount_err="some other error", kiod=True)
    assert "kquitapp6" not in try_mount_garmin(other)[1]
    assert try_mount_garmin(lambda argv: (0, "", "")) == (False, "no Garmin MTP device reported by gio")


def test_locate_uses_mounter_only_without_path(config, tmp_path: Path, garmin_tree: Path):
    calls: list[str] = []

    def mounter():
        calls.append("mount")
        return False, "nothing"

    root, message = locate_garmin(config, garmin_tree, mounter=mounter)
    assert root == garmin_tree and message is None and calls == []

    root, message = locate_garmin(config, None, roots=lambda: [tmp_path / "nowhere"], mounter=mounter)
    assert root is None and message == "nothing" and calls == ["mount"]

    # a successful mount is followed by a second search
    appearing: list[Path] = []

    def mount_ok():
        appearing.append(garmin_tree.parent)
        return True, "mounted"

    root, message = locate_garmin(config, None, roots=lambda: list(appearing), mounter=mount_ok)
    assert root == garmin_tree and message == "mounted"


def test_import_reports_mount_message(conn, config, tmp_path: Path):
    stats = import_garmin(conn, config, roots=lambda: [tmp_path / "nowhere"], mounter=lambda: (False, "gio failed"))
    assert stats.root is None and stats.mount_message == "gio failed"
