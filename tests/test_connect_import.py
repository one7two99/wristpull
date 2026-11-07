"""Importing the Garmin Connect account export (phase 1: the uploaded FIT files)."""

import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from connect_export import ACCOUNT, build_export_dir, build_export_zip, export_fits, uploads_zip
from wristpull.importers.connect import SOURCE, display_name, import_garmin_export, iter_export_fit
from wristpull.importers.garmin import import_garmin, kind_for_fit_type
from wristpull.merge import merged_daily


def test_display_name_strips_the_account_prefix():
    assert display_name(f"{ACCOUNT}_7102332115.fit") == "7102332115.fit"
    assert display_name(f"DI_CONNECT/x/{ACCOUNT}_1.fit") == "1.fit"
    assert display_name("2026-10-06-08-00-00.fit") == "2026-10-06-08-00-00.fit"


def test_kind_for_fit_type():
    assert [kind_for_fit_type(t) for t in ("monitoring_b", "activity", "49", "68", "44", "41", None)] == ["monitor", "activity", "sleep", "hrv", "metrics", "other", "other"]


def test_iter_export_fit_reads_zip_parts_inner_zip_and_directories(tmp_path: Path):
    portal = build_export_zip(tmp_path / "export.zip", parts=2, junk=b"")
    items = list(iter_export_fit(portal))
    assert sorted(i.name for i in items) == ["7102332115.fit", "7102332116.fit", "7102332117.fit", "7102332118.fit", "7102332119.fit"]
    assert {i.origin.split("/")[0] for i in items} == {"UploadedFiles_0-_Part1.zip", "UploadedFiles_0-_Part2.zip"}
    assert all("@" not in i.name and "@" not in i.origin for i in items)
    inner = tmp_path / "UploadedFiles_0-_Part1.zip"
    inner.write_bytes(uploads_zip(export_fits()[:2]))
    assert [i.name for i in iter_export_fit(inner)] == ["7102332115.fit", "7102332116.fit"]
    extracted = build_export_dir(tmp_path / "extracted")
    assert len(list(iter_export_fit(extracted))) == 5
    loose = build_export_dir(tmp_path / "loose", loose=True)
    assert [i.origin for i in iter_export_fit(loose)][0] == "DI-Connect-Uploaded-Files/7102332115.fit"
    with pytest.raises(FileNotFoundError):
        list(iter_export_fit(tmp_path / "missing"))
    with pytest.raises(zipfile.BadZipFile):
        (tmp_path / "bad.zip").write_bytes(b"nope")
        list(iter_export_fit(tmp_path / "bad.zip"))


def test_import_export_archives_by_fit_type_and_is_idempotent(conn, config, tmp_path: Path):
    portal = build_export_zip(tmp_path / "export.zip")
    logs: list[str] = []
    stats = import_garmin_export(conn, config, portal, log=logs.append, progress_every=2)
    assert (stats.found, stats.new, stats.skipped, stats.errors) == (6, 6, 0, 1) and stats.root == str(portal)
    assert stats.error_files[0][0] == "UploadedFiles_0-_Part1.zip/broken.fit"
    rows = conn.execute("SELECT kind, original_name, origin_path, source, parse_status, fit_file_type FROM raw_files ORDER BY original_name").fetchall()
    assert [tuple(r) for r in rows] == [
        ("monitor", "7102332115.fit", "UploadedFiles_0-_Part1.zip/7102332115.fit", SOURCE, "ok", "monitoring_b"),
        ("sleep", "7102332116.fit", "UploadedFiles_0-_Part1.zip/7102332116.fit", SOURCE, "ok", "49"),
        ("activity", "7102332117.fit", "UploadedFiles_0-_Part1.zip/7102332117.fit", SOURCE, "ok", "activity"),
        ("metrics", "7102332118.fit", "UploadedFiles_0-_Part1.zip/7102332118.fit", SOURCE, "ok", "44"),
        ("hrv", "7102332119.fit", "UploadedFiles_0-_Part1.zip/7102332119.fit", SOURCE, "ok", "68"),
        ("other", "broken.fit", "UploadedFiles_0-_Part1.zip/broken.fit", SOURCE, "error", None),
    ]
    archived = sorted(p.relative_to(config.raw_dir).as_posix() for p in config.raw_dir.rglob("*.fit"))
    assert archived[0].startswith("garmin_connect/2026/activity/") and all("@" not in a for a in archived)
    assert stats.rows["heart_rate"] == 672 and stats.rows["sleep"] == 1 and stats.rows["activities"] == 1
    assert conn.execute("SELECT COUNT(*) FROM daily_summary WHERE source = ?", (SOURCE,)).fetchone()[0] >= 1
    assert any(line.startswith("  2 files:") for line in logs) and any("rebuilding" in line for line in logs)
    again = import_garmin_export(conn, config, portal)
    assert (again.found, again.new, again.skipped, again.errors) == (6, 0, 6, 0)
    assert conn.execute("SELECT COUNT(*) FROM heart_rate").fetchone()[0] == 672


def test_files_already_archived_from_the_watch_are_skipped(conn, config, garmin_tree: Path, tmp_path: Path):
    import_garmin(conn, config, path=garmin_tree)  # the same bytes the export carries for the monitor file
    stats = import_garmin_export(conn, config, build_export_zip(tmp_path / "export.zip", junk=b""))
    assert stats.found == 5 and stats.skipped >= 1 and stats.new == 5 - stats.skipped
    assert conn.execute("SELECT source FROM raw_files WHERE original_name = 'M6A10500.FIT'").fetchone()[0] == "garmin_usb"
    assert conn.execute("SELECT COUNT(*) FROM raw_files WHERE original_name = '7102332115.fit'").fetchone()[0] == 0


def test_dry_run_and_stop(conn, config, tmp_path: Path):
    portal = build_export_zip(tmp_path / "export.zip", junk=b"")
    stats = import_garmin_export(conn, config, portal, dry_run=True)
    assert (stats.found, stats.new, stats.skipped) == (5, 5, 0) and conn.execute("SELECT COUNT(*) FROM raw_files").fetchone()[0] == 0
    calls = iter([False, False, True])
    stats = import_garmin_export(conn, config, portal, stop=lambda: next(calls, True))
    assert stats.stopped and stats.found == 2 and stats.new == 2


def test_connect_only_fills_gaps_in_the_merged_view(conn, config, garmin_tree: Path, tmp_path: Path):
    import_garmin(conn, config, path=garmin_tree)  # 2026-10-06 from the watch
    older = export_fits(datetime(2026, 9, 1, 22, 0, tzinfo=UTC))  # a day only the export knows
    import_garmin_export(conn, config, build_export_zip(tmp_path / "export.zip", fits=older, junk=b""))
    days = {d.date: d for d in merged_daily(conn, "2026-09-01", "2026-10-07")}
    assert "2026-09-02" in days and days["2026-09-02"].sources == [SOURCE] and days["2026-09-02"].garmin["steps"]
    assert days["2026-10-06"].sources == ["garmin_usb"]
    # the same day from both sources: the watch wins, the export fills nothing it already has
    import_garmin_export(conn, config, build_export_zip(tmp_path / "export2.zip", junk=b""))
    both = {d.date: d for d in merged_daily(conn, "2026-10-06", "2026-10-06")}["2026-10-06"]
    assert both.sources == [SOURCE, "garmin_usb"]
    usb = conn.execute("SELECT steps FROM daily_summary WHERE source = 'garmin_usb' AND local_date = '2026-10-06'").fetchone()[0]
    assert both.garmin["steps"] == usb


def test_cli_import_garmin_export(config, tmp_path: Path, capsys):
    from wristpull.cli import main
    from wristpull.config import write_config

    cfg_file = write_config(config, tmp_path / "config.toml")
    portal = build_export_zip(tmp_path / "export.zip")
    assert main(["--config", str(cfg_file), "import", "garmin-export", str(portal), "--dry-run"]) == 0
    assert "dry run:" in capsys.readouterr().out
    assert main(["--config", str(cfg_file), "import", "garmin-export", str(portal)]) == 2  # the broken file counts as an error
    out, err = capsys.readouterr()
    assert "6 FIT files, 6 new, 0 already archived, 1 errors" in out and "error UploadedFiles_0-_Part1.zip/broken.fit" in err
    assert main(["--config", str(cfg_file), "import", "garmin-export", str(tmp_path / "nope.zip")]) == 2
    assert "garmin-export:" in capsys.readouterr().err
