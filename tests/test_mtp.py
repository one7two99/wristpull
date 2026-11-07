"""The MTP layer against verbatim captures of the mtp-tools (tests/fixtures/mtp, fenix 6X Pro, serials replaced)."""

from pathlib import Path

import pytest

from wristpull.config import Config
from wristpull.mtp import (
    MTP_BUSY_HINT,
    MtpBusyError,
    MtpError,
    MtpTools,
    NoMtpDeviceError,
    busy,
    folder_id,
    mtp_device_present,
    no_device,
    parse_detect,
    parse_files,
    parse_folders,
    probe_mtp,
    resolve_access,
)

FIXTURES = Path(__file__).parent / "fixtures" / "mtp"


def fx(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_parse_folders_builds_paths():
    folders = parse_folders(fx("folders.txt"))
    assert folders[16777216] == "/GARMIN"
    assert folders[16777261] == "/GARMIN/Monitor" and folders[16777279] == "/GARMIN/NewFiles"
    assert folders[16777500] == "/GARMIN/Workouts/Schedule" and folders[16777582] == "/GARMIN/Apps/DATA/AUXFILE"
    assert folder_id(folders, "garmin/newfiles") == (16777279, "/GARMIN/NewFiles") and folder_id(folders, "/GARMIN/Nope") is None
    assert parse_folders(fx("folders-busy.txt")) == {}


def test_parse_files_joins_parent_paths():
    folders = parse_folders(fx("folders.txt"))
    files = parse_files(fx("files.txt"), folders)
    monitor = [f for f in files if f.path.startswith("/GARMIN/Monitor/")]
    assert [(f.name, f.size) for f in monitor] == [("MB700000.FIT", 7819), ("MB795515.FIT", 23735)]
    assert monitor[0].id == 16777511 and monitor[0].parent_id == 16777261
    xml = next(f for f in files if f.name == "GarminDevice.xml")
    assert xml.path == "/GARMIN/GarminDevice.xml" and xml.size == 48640
    sent = parse_files(fx("files-after-send.txt"), folders)
    assert any(f.path == "/GARMIN/NewFiles/wristpull-spike-test.fit" and f.size == 286 for f in sent)


def test_parse_detect():
    info = parse_detect(fx("detect.txt"))
    assert (info.vendor, info.model, info.serial, info.version) == ("Garmin", "fenix 6X Pro", "0000deadbeef", "2802")
    assert info.storages == ("Primary",) and info.label() == "fenix 6X Pro (0000deadbeef)"
    empty = parse_detect(fx("detect-busy.txt"))
    assert empty.model is None and empty.serial is None


def test_no_device_and_busy_markers():
    assert not no_device(fx("detect.txt")) and not busy(fx("detect.txt"))
    for name in ("detect-unplugged.txt", "folders-unplugged.txt", "files-unplugged.txt", "getfile-unplugged.txt", "getfile-nodevnode.txt"):
        assert no_device(fx(name)) and not busy(fx(name)), name
    assert parse_folders(fx("folders-unplugged.txt")) == {} and parse_files(fx("files-unplugged.txt"), {}) == []
    assert busy(fx("detect-busy.err")) and busy(fx("folders-busy.err")) and busy(fx("detect-nodevnode.err"))


class ScriptedRunner:
    """Answers each tool from the fixtures; records the argv list."""

    def __init__(self, answers: dict[str, tuple[int, str, str]]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        return self.answers[argv[0]]


def watch_runner(**overrides) -> ScriptedRunner:
    answers = {"mtp-detect": (0, fx("detect.txt"), ""), "mtp-folders": (0, fx("folders.txt"), ""), "mtp-files": (0, fx("files.txt"), "")}
    answers.update(overrides)
    return ScriptedRunner(answers)


def test_tools_snapshot_is_cached_and_invalidated(tmp_path: Path):
    runner = watch_runner(**{"mtp-delfile": (0, fx("delfile-ok.txt"), "")})
    mtp = MtpTools(runner)
    assert mtp.device_info().model == "fenix 6X Pro"
    assert [f.name for f in mtp.list("/GARMIN/Monitor")] == ["MB700000.FIT", "MB795515.FIT"]
    assert mtp.list("/garmin/sleep")[0].name == "GB785248.fit" and mtp.list("/GARMIN/Missing") == []
    assert mtp.folders()[16777216] == "/GARMIN"
    assert [c[0] for c in runner.calls] == ["mtp-detect", "mtp-folders", "mtp-files"]  # one snapshot for three lookups
    mtp.delete(16777751)
    mtp.list("/GARMIN/Monitor")
    assert [c[0] for c in runner.calls][3:] == ["mtp-delfile", "mtp-folders", "mtp-files"] and runner.calls[3] == ["mtp-delfile", "-n", "16777751"]
    assert mtp.device_info().model == "fenix 6X Pro" and [c[0] for c in runner.calls].count("mtp-detect") == 1


def test_get_checks_size_and_removes_a_partial_file(tmp_path: Path):
    def getfile(argv: list[str]) -> tuple[int, str, str]:
        Path(argv[2]).write_bytes(b"x" * 7819)
        return 0, fx("getfile-ok.txt"), ""

    class Runner(ScriptedRunner):
        def __call__(self, argv):
            self.calls.append(argv)
            return getfile(argv) if argv[0] == "mtp-getfile" else self.answers[argv[0]]

    mtp = MtpTools(Runner(watch_runner().answers))
    ids = {f.name: f.id for f in mtp.list("/GARMIN/Monitor")}  # the snapshot carries the sizes
    dest = tmp_path / "staging" / "Monitor" / "MB700000.FIT"
    mtp.get(ids["MB700000.FIT"], dest)
    assert dest.stat().st_size == 7819
    with pytest.raises(MtpError, match="got 7819 bytes, the listing said 23735"):
        mtp.get(ids["MB795515.FIT"], tmp_path / "other.FIT")
    assert not (tmp_path / "other.FIT").exists()
    bad = MtpTools(watch_runner(**{"mtp-getfile": (1, fx("getfile-bad-id.txt"), "")}))
    with pytest.raises(MtpError, match="Error getting file from MTP device"):
        bad.get(99, tmp_path / "bad.fit")


def test_send_stages_the_name_and_needs_new_file_id(tmp_path: Path):
    src = tmp_path / "local-name.fit"
    src.write_bytes(b"fit")
    seen: dict[str, Path] = {}

    class Runner(ScriptedRunner):
        def __call__(self, argv):
            self.calls.append(argv)
            if argv[0] == "mtp-sendfile":
                seen["staged"] = Path(argv[1])
                seen["existed"] = Path(argv[1]).exists()
                return 0, fx("sendfile-ok.txt"), ""
            return self.answers[argv[0]]

    runner = Runner(watch_runner().answers)
    mtp = MtpTools(runner)
    assert mtp.send(src, "/garmin/newfiles", "2026-11-08-run.fit") == 16777751
    assert seen["staged"].name == "2026-11-08-run.fit" and seen["existed"] and not seen["staged"].exists()
    assert runner.calls[-1][2] == "/GARMIN/NewFiles"  # the device's spelling of the folder
    with pytest.raises(MtpError, match="no folder /GARMIN/DoesNotExist"):
        mtp.send(src, "/GARMIN/DoesNotExist", "x.fit")
    skipped = MtpTools(watch_runner(**{"mtp-sendfile": (0, fx("sendfile-nofolder.txt"), "")}))
    with pytest.raises(MtpError, match="Parent folder could not be found"):
        skipped.send(src, "/GARMIN/NewFiles", "x.fit")


def test_delete_failure_and_missing_tools():
    failing = MtpTools(watch_runner(**{"mtp-delfile": (1, fx("delfile-missing.txt"), fx("delfile-missing.err"))}))
    with pytest.raises(MtpError, match="could not delete object|Failed to delete"):
        failing.delete(16777751)
    missing = MtpTools(lambda argv: (127, "", "[Errno 2] No such file or directory: 'mtp-detect'"))
    with pytest.raises(MtpError, match="apt install mtp-tools"):
        missing.device_info()


def test_no_device_busy_and_probe_messages():
    unplugged = MtpTools(lambda argv: (0, "libmtp version: 1.1.22\n\nListing raw device(s)\n   No raw devices found.\n", ""))
    with pytest.raises(NoMtpDeviceError):
        unplugged.device_info()
    assert probe_mtp(unplugged) == (None, "no MTP device found (is the watch plugged in and awake?)")
    held = MtpTools(lambda argv: (0, fx("detect-busy.txt"), fx("detect-busy.err")))
    with pytest.raises(MtpBusyError):
        held.folders()
    assert probe_mtp(held) == (None, MTP_BUSY_HINT)
    assert probe_mtp(MtpTools(watch_runner())) [1] == "MTP: fenix 6X Pro (0000deadbeef)"
    assert mtp_device_present(watch_runner()) and not mtp_device_present(unplugged._runner)


def test_default_runner_is_looked_up_at_call_time(monkeypatch):
    import wristpull.mtp as mtp_module

    with pytest.raises(AssertionError, match="real mtp-tools"):  # the conftest guard
        MtpTools().device_info()
    monkeypatch.setattr(mtp_module, "run_command", lambda argv: (0, fx("detect.txt"), ""))
    assert MtpTools().device_info().serial == "0000deadbeef"


def test_resolve_access():
    assert resolve_access(Config(garmin_access="auto"), {}) == "gvfs"
    assert resolve_access(Config(garmin_access="auto"), {"WRISTPULL_IN_CONTAINER": "1"}) == "mtp"
    assert resolve_access(Config(garmin_access="gvfs"), {"WRISTPULL_IN_CONTAINER": "1"}) == "gvfs"
    assert resolve_access(Config(garmin_access="mtp"), {}) == "mtp"
