from pathlib import Path

from wristpull.garmin_usb import garmin_dir, list_fit_files, read_device_info


def test_find_root_case_insensitive(tmp_path: Path, garmin_tree: Path):
    mount = garmin_tree.parent
    assert garmin_dir(mount) == garmin_tree
    assert garmin_dir(garmin_tree) == garmin_tree
    assert garmin_dir(tmp_path / "nope") is None
    lower = tmp_path / "other" / "garmin"
    lower.mkdir(parents=True)
    assert garmin_dir(lower.parent) == lower and garmin_dir(tmp_path / "x") is None


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
