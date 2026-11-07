from pathlib import Path

from wristpull.garmin_usb import candidate_roots, find_garmin_root, garmin_dir, list_fit_files, read_device_info


def test_find_root_case_insensitive(tmp_path: Path, garmin_tree: Path):
    mount = garmin_tree.parent
    assert garmin_dir(mount) == garmin_tree
    assert garmin_dir(garmin_tree) == garmin_tree
    assert garmin_dir(tmp_path / "nope") is None
    lower = tmp_path / "other" / "garmin"
    lower.mkdir(parents=True)
    assert find_garmin_root([tmp_path / "x", lower.parent]) == lower
    assert find_garmin_root([tmp_path]) is None


def test_candidate_roots_uses_extra_and_skips_missing(tmp_path: Path):
    roots = candidate_roots(user="nobody-here", uid=999999, extra=[tmp_path])
    assert roots[0] == tmp_path


def test_list_fit_files_tolerates_missing_folders(garmin_tree: Path):
    files = list_fit_files(garmin_tree, ["Activity", "Monitor", "Sleep", "Metrics", "HRVStatus", "Totals"])
    kinds = sorted((f.kind, f.name) for f in files)
    assert kinds == [
        ("activity", "2026-10-06-08-00-00.fit"),
        ("hrv", "HRV00001.FIT"),
        ("metrics", "MET00001.FIT"),
        ("monitor", "M6A10500.FIT"),
        ("sleep", "S6A10530.FIT"),
    ]
    assert all(f.size > 0 and f.mtime.tzinfo is not None for f in files)
    assert list_fit_files(garmin_tree / "missing", ["Activity"]) == []


def test_read_device_info(garmin_tree: Path, tmp_path: Path):
    info = read_device_info(garmin_tree)
    assert info == {"part_number": "006-B3289-00", "model": "fenix 6 Pro", "unit_id": "3999999999"}
    assert read_device_info(tmp_path) == {}


def test_eject_garmin_only_for_mtp_mounts(tmp_path):
    from wristpull.garmin_usb import eject_garmin, mtp_url_of

    mtp_root = Path("/run/user/1000/gvfs/mtp:host=091e_4cdb_0000<id>/Primary/GARMIN")
    assert mtp_url_of(mtp_root) == "mtp://091e_4cdb_0000<id>/" and mtp_url_of(tmp_path) is None
    calls = []
    ok, msg = eject_garmin(mtp_root, lambda argv: (calls.append(argv), (0, "", ""))[1])
    assert ok and calls == [["gio", "mount", "-u", "mtp://091e_4cdb_0000<id>/"]] and "ejected" in msg
    ok, msg = eject_garmin(mtp_root, lambda argv: (1, "", "busy"))
    assert not ok and "busy" in msg
    ok, msg = eject_garmin(tmp_path, lambda argv: (0, "", ""))
    assert not ok and "nothing to eject" in msg


def test_current_user_falls_back_to_the_uid(monkeypatch):
    import getpass

    from wristpull.garmin_usb import candidate_roots, current_user

    def no_entry() -> str:
        raise KeyError("getpwuid(): uid not found")

    monkeypatch.setattr(getpass, "getuser", no_entry)
    assert current_user().isdigit()
    assert candidate_roots() == [] or all(p.exists() for p in candidate_roots())
