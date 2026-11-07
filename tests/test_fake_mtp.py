from pathlib import Path

import pytest

from fake_mtp import FakeMtp, fake_mtp_runner
from wristpull.mtp import MtpError, MtpTools, NoMtpDeviceError, probe_mtp


def test_fake_serves_the_tree_with_stable_ids(garmin_tree: Path, tmp_path: Path):
    fake = FakeMtp(garmin_tree.parent)
    assert fake.device_info().model == "fenix 6 Pro"
    folders = fake.folders()
    assert "/GARMIN" in folders.values() and "/GARMIN/Monitor" in folders.values()
    monitor = fake.list("/garmin/monitor")
    assert [f.name for f in monitor] == ["M6A10500.FIT"] and monitor[0].path == "/GARMIN/Monitor/M6A10500.FIT"
    assert fake.list("/GARMIN/Nope") == []
    before = dict(fake.ids)
    dest = tmp_path / "out" / "M6A10500.FIT"
    fake.get(monitor[0].id, dest)
    assert dest.read_bytes() == (garmin_tree / "Monitor" / "M6A10500.FIT").read_bytes()
    assert fake.fetched == [(monitor[0].id, "/GARMIN/Monitor/M6A10500.FIT")]
    with pytest.raises(MtpError):
        fake.get(1, tmp_path / "x")
    fake.fail_get.add("/GARMIN/Monitor/M6A10500.FIT")
    with pytest.raises(MtpError):
        fake.get(monitor[0].id, dest)
    assert all(fake.ids[k] == v for k, v in before.items())


def test_fake_send_delete_and_absent_device(garmin_tree: Path, tmp_path: Path):
    fake = FakeMtp(garmin_tree.parent)
    (garmin_tree / "NewFiles").mkdir()
    src = tmp_path / "w.fit"
    src.write_bytes(b"fit")
    new_id = fake.send(src, "/GARMIN/NewFiles", "2026-11-08-run.fit")
    assert (garmin_tree / "NewFiles" / "2026-11-08-run.fit").read_bytes() == b"fit" and fake.sent == [("/GARMIN/NewFiles", "2026-11-08-run.fit")]
    assert fake.list("/GARMIN/NewFiles")[0].id == new_id
    with pytest.raises(AssertionError, match="already exists"):
        fake.send(src, "/GARMIN/NewFiles", "2026-11-08-run.fit")
    with pytest.raises(MtpError, match="no folder"):
        fake.send(src, "/GARMIN/Nope", "x.fit")
    fake.delete(new_id)
    assert not (garmin_tree / "NewFiles" / "2026-11-08-run.fit").exists() and fake.deleted == [new_id]
    with pytest.raises(MtpError):
        fake.delete(new_id)
    absent = FakeMtp(garmin_tree.parent, present=False)
    with pytest.raises(NoMtpDeviceError):
        absent.list("/GARMIN")
    assert probe_mtp(FakeMtp(garmin_tree.parent, busy=True))[0] is None


def test_fake_runner_round_trips_through_the_real_parsers(garmin_tree: Path, tmp_path: Path):
    fake = FakeMtp(garmin_tree.parent)
    (garmin_tree / "NewFiles").mkdir()
    mtp = MtpTools(fake_mtp_runner(fake))
    assert mtp.device_info().serial == "0000deadbeef"
    assert mtp.folders() == fake.folders()
    sleep = mtp.list("/GARMIN/Sleep")
    assert [f.name for f in sleep] == ["S6A10530.FIT"] and sleep[0].id == fake.ids["/GARMIN/Sleep/S6A10530.FIT"]
    mtp.get(sleep[0].id, tmp_path / "S.FIT")
    assert (tmp_path / "S.FIT").read_bytes() == (garmin_tree / "Sleep" / "S6A10530.FIT").read_bytes()
    with pytest.raises(MtpError, match="Error getting file"):
        mtp.get(5, tmp_path / "bad")
    src = tmp_path / "w.fit"
    src.write_bytes(b"fit")
    new_id = mtp.send(src, "/GARMIN/NewFiles", "w2.fit")
    assert [f.name for f in mtp.list("/GARMIN/NewFiles")] == ["w2.fit"] and fake.sent == [("/GARMIN/NewFiles", "w2.fit")]
    with pytest.raises(MtpError, match="no folder"):  # refused before the tool runs
        mtp.send(src, "/GARMIN/Nope", "x.fit")
    assert fake_mtp_runner(fake)(["mtp-sendfile", str(src), "/GARMIN/Nope"])[1].endswith("Parent folder could not be found, skipping\n")
    mtp.delete(new_id)
    assert mtp.list("/GARMIN/NewFiles") == []
    with pytest.raises(MtpError, match="delete"):
        mtp.delete(new_id)
    fake.present = False
    with pytest.raises(NoMtpDeviceError):
        MtpTools(fake_mtp_runner(fake)).folders()
