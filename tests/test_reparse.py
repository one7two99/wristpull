from pathlib import Path

import pytest

from gb_db import build_gb_db
from wristpull.cli import main
from wristpull.daily import rebuild_daily_summary
from wristpull.db import table_count
from wristpull.importers.garmin import import_garmin
from wristpull.importers.phone import import_gadgetbridge_db
from wristpull.importers.reparse import delete_rows_of, reparse, select_raw_files


@pytest.fixture
def cfg_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(f'[storage]\ndata_dir = "{(tmp_path / "Health").as_posix()}"\n[time]\ntimezone = "Europe/Berlin"\n', "utf-8")
    return path


def test_reparse_replaces_rows_and_rebuilds_summary(conn, config, garmin_tree: Path, tmp_path: Path):
    import_garmin(conn, config, path=garmin_tree)
    db = build_gb_db(tmp_path / "Gadgetbridge.db")
    import_gadgetbridge_db(conn, config, db)
    rebuild_daily_summary(conn, source="gadgetbridge_zepp")  # the library import leaves this to the caller
    before = {t: table_count(conn, t) for t in ("activities", "activity_records", "sleep", "sleep_stages", "heart_rate", "steps", "daily_summary")}
    # make a sample row wrong on purpose; a reparse must restore it
    conn.execute("UPDATE heart_rate SET bpm = 1 WHERE source = 'gadgetbridge_zepp'")
    conn.commit()
    logs: list[str] = []
    stats = reparse(conn, config, source="gadgetbridge", log=logs.append)
    assert stats.selected == 1 and stats.ok == 1 and stats.errors == 0
    assert stats.sources == {"gadgetbridge_zepp"} and stats.deleted > 0
    assert conn.execute("SELECT MAX(bpm) FROM heart_rate WHERE source = 'gadgetbridge_zepp'").fetchone()[0] > 1
    after = {t: table_count(conn, t) for t in before}
    assert after == before, (before, after)
    assert any("~ gadgetbridge/" in line for line in logs)
    # all Garmin files, filtered by kind
    stats = reparse(conn, config, source="garmin_usb", kind="activity")
    assert stats.selected == 1 and stats.ok == 1
    assert {t: table_count(conn, t) for t in before} == before
    assert conn.execute("SELECT parse_status FROM raw_files WHERE kind = 'activity'").fetchone()[0] == "ok"


def test_reparse_reports_missing_and_corrupt_files(conn, config, garmin_tree: Path):
    import_garmin(conn, config, path=garmin_tree)
    row = select_raw_files(conn, kind="monitor")[0]
    (config.raw_dir / row["archive_path"]).chmod(0o644)
    (config.raw_dir / row["archive_path"]).write_bytes(b"\x0e\x10not a fit file")
    stats = reparse(conn, config, ids=[int(row["id"])])
    assert stats.errors == 1 and stats.ok == 0
    assert conn.execute("SELECT parse_status FROM raw_files WHERE id = ?", (row["id"],)).fetchone()[0] == "error"
    assert table_count(conn, "heart_rate", "raw_file_id = ?", (row["id"],)) == 0
    stats = reparse(conn, config, errors_only=True)
    assert stats.selected == 1 and stats.errors == 1
    conn.execute("UPDATE raw_files SET archive_path = 'garmin_usb/2026/monitor/gone.FIT' WHERE id = ?", (row["id"],))
    conn.commit()
    stats = reparse(conn, config, ids=[int(row["id"])])
    assert stats.error_files == [("garmin_usb/2026/monitor/gone.FIT", "missing in the archive")]


def test_delete_rows_of_cascades(conn, config, garmin_tree: Path):
    import_garmin(conn, config, path=garmin_tree)
    activity_file = select_raw_files(conn, kind="activity")[0]
    deleted = delete_rows_of(conn, int(activity_file["id"]))
    assert deleted > 300  # records + laps + the activity itself
    assert table_count(conn, "activity_records") == 0 and table_count(conn, "activities") == 0
    assert table_count(conn, "heart_rate") > 0  # other files untouched


def test_reparse_cli(cfg_file: Path, garmin_tree: Path, capsys):
    assert main(["--config", str(cfg_file), "import", "garmin", "--path", str(garmin_tree)]) == 0
    capsys.readouterr()
    assert main(["--config", str(cfg_file), "reparse"]) == 2
    assert main(["--config", str(cfg_file), "reparse", "--kind", "sleep"]) == 0
    out = capsys.readouterr().out
    assert "1 files selected, 1 re-parsed, 0 errors" in out and "sleep=1" in out
    assert main(["--config", str(cfg_file), "-q", "reparse", "--all"]) == 0
    assert "5 files selected, 5 re-parsed" in capsys.readouterr().out
