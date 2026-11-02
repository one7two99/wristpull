from pathlib import Path

import pytest

from wristpull.cli import main


@pytest.fixture
def cfg_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(f'[storage]\ndata_dir = "{tmp_path / "Health"}"\n[time]\ntimezone = "Europe/Berlin"\n', "utf-8")
    return path


def test_import_status_and_inspect(cfg_file: Path, garmin_tree: Path, capsys):
    assert main(["--config", str(cfg_file), "import", "garmin", "--path", str(garmin_tree)]) == 0
    out = capsys.readouterr().out
    assert "5 FIT files, 5 new, 0 already archived, 0 errors" in out
    assert "activities=1" in out

    assert main(["--config", str(cfg_file), "status"]) == 0
    out = capsys.readouterr().out
    assert "garmin_usb" in out and "monitor" in out
    assert "heart_rate" in out and "672" in out
    assert "daily_summary" in out

    fit_file = garmin_tree / "Monitor" / "M6A10500.FIT"
    assert main(["--config", str(cfg_file), "inspect", str(fit_file), "--messages", "stress_level", "--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert "stress_level (#227): 96" in out
    assert out.count("[stress_level]") == 2


def test_import_without_device(cfg_file: Path, tmp_path: Path, capsys):
    assert main(["--config", str(cfg_file), "import", "garmin", "--path", str(tmp_path / "empty")]) == 1
    assert "No Garmin device found" in capsys.readouterr().err


def test_config_init_and_show(tmp_path: Path, capsys):
    cfg = tmp_path / "c" / "config.toml"
    assert main(["--config", str(cfg), "config", "--init"]) == 0
    assert cfg.exists()
    assert main(["--config", str(cfg), "config"]) == 0
    assert "[storage]" in capsys.readouterr().out


def test_export_requires_dates(cfg_file: Path):
    with pytest.raises(SystemExit):
        main(["--config", str(cfg_file), "export"])


def test_import_gadgetbridge_cli(cfg_file: Path, tmp_path: Path, capsys):
    from gb_db import build_gb_db

    db = build_gb_db(tmp_path / "Gadgetbridge.db")
    assert main(["--config", str(cfg_file), "import", "gadgetbridge", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "Amazfit Helio Strap" in out and "heart_rate=295" in out
    assert main(["--config", str(cfg_file), "import", "gadgetbridge"]) == 2


def test_pull_cli_without_adb_device(cfg_file: Path, capsys, monkeypatch):
    from wristpull import cli
    from wristpull.adb import NoDeviceError

    def boom(*args, **kwargs):
        raise NoDeviceError("no authorized device connected")

    monkeypatch.setattr(cli, "pull_phone", boom)
    assert main(["--config", str(cfg_file), "pull", "phone"]) == 1
    assert "no authorized device" in capsys.readouterr().err


def test_tui_without_textual_explains(cfg_file: Path, capsys, monkeypatch):
    from wristpull import cli

    def missing():
        raise ImportError("No module named 'textual'")

    monkeypatch.setattr(cli, "_load_tui", missing)
    assert main(["--config", str(cfg_file), "tui"]) == 1
    assert "uv sync --extra tui" in capsys.readouterr().err


def test_tui_command_runs_app(cfg_file: Path, monkeypatch):
    from wristpull import cli

    seen = {}

    def fake_run(config):
        seen["data_dir"] = config.data_dir
        return 0

    monkeypatch.setattr(cli, "_load_tui", lambda: fake_run)
    assert main(["--config", str(cfg_file), "tui"]) == 0
    assert seen["data_dir"].name == "Health"


def test_workout_commands(cfg_file: Path, tmp_path: Path, garmin_tree: Path, capsys):
    base = ["--config", str(cfg_file), "workout"]
    assert main([*base, "list"]) == 1
    assert "no workout files" in capsys.readouterr().out
    assert main([*base, "new", "--date", "2026-11-05", "--name", "Easy run"]) == 0
    path = Path(capsys.readouterr().out.strip())
    assert path.name == "2026-11-05-easy-run.md" and path.exists()
    assert main([*base, "new", "--date", "2026-11-06", "--template", "distance", "--metres", "8000"]) == 0
    capsys.readouterr()
    assert main([*base, "new", "--date", "2026-11-06", "--template", "distance", "--metres", "8000"]) == 2  # exists
    assert "file exists" in capsys.readouterr().err
    assert main([*base, "check", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Easy run (2026-11-05) ok" in out and "1 warmup 10:00  HR <= 130" in out and "2 active 40:00  HR Z2" in out
    bad = path.parent / "bad.md"
    bad.write_text("---\ndate: 2026-11-07\nname: Bad\n---\n- sprint 5 min\n", "utf-8")
    assert main([*base, "check", str(bad)]) == 2
    assert "unknown intensity" in capsys.readouterr().err
    assert main([*base, "list"]) == 0
    out = capsys.readouterr().out
    assert "2026-11-05  Easy run" in out and "55 min" in out and "draft" in out and "✗ unknown intensity" in out and "8000 m" in out
    assert main([*base, "push"]) == 2
    assert main([*base, "push", "--date", "2026-11-05", "--dry-run", "--schedule"]) == 0
    out = capsys.readouterr().out
    assert "dry run: 1 workout files written to" in out and "schedule" in out
    assert main([*base, "push", str(path), "--path", str(tmp_path / "nowhere")]) == 1
    assert "No Garmin device found" in capsys.readouterr().err
    assert main([*base, "push", str(path), str(bad), "--path", str(garmin_tree)]) == 2  # the bad one is reported
    captured = capsys.readouterr()
    assert "1 workout files written to" in captured.out and "Unplug the watch" in captured.out and "Bad:" in captured.err
    assert len(list((garmin_tree / "NewFiles").glob("*.fit"))) == 1
    assert main([*base, "list"]) == 0
    assert "pushed" in capsys.readouterr().out
    assert main([*base, "verify", "--path", str(garmin_tree)]) == 2  # pushed but not under Workouts yet
    assert "1 pushed but not found" in capsys.readouterr().out
    (garmin_tree / "Workouts").mkdir()
    (garmin_tree / "Workouts" / "W.FIT").write_bytes(next((garmin_tree / "NewFiles").glob("*.fit")).read_bytes())
    assert main([*base, "verify", "--path", str(garmin_tree)]) == 0
    assert "1 newly confirmed" in capsys.readouterr().out
    assert main([*base, "verify", "--path", str(tmp_path / "nowhere")]) == 1
    capsys.readouterr()
    assert main([*base, "from-plan"]) == 1
    assert "no weekly plan" in capsys.readouterr().err
    assert main([*base, "push", "--upcoming", "--dry-run"]) in (0, 2)


def test_workout_watch_remove_and_force(cfg_file: Path, tmp_path: Path, garmin_tree: Path, capsys, monkeypatch):
    base = ["--config", str(cfg_file), "workout"]
    assert main([*base, "watch", "--path", str(tmp_path / "nowhere")]) == 1
    assert "No Garmin device found" in capsys.readouterr().err
    assert main([*base, "watch", "--path", str(garmin_tree)]) == 0
    assert "0 of ~25 workouts on the watch" in capsys.readouterr().out
    assert main([*base, "new", "--date", "2099-01-05", "--name", "Future run"]) == 0
    capsys.readouterr()
    assert main([*base, "push", "--upcoming", "--path", str(garmin_tree), "--folder", "Workouts"]) == 0
    capsys.readouterr()
    assert main([*base, "push", "--upcoming", "--path", str(garmin_tree)]) == 2  # nothing left without --force
    assert "nothing to push" in capsys.readouterr().err
    assert main([*base, "push", "--upcoming", "--force", "--path", str(garmin_tree), "--folder", "Workouts"]) == 0
    capsys.readouterr()
    assert main([*base, "watch", "--path", str(garmin_tree)]) == 0
    out = capsys.readouterr().out
    assert out.count("Future run") == 2 and "wristpull" in out and "2 of ~25 workouts on the watch" in out
    # interactive removal: answer no, then yes
    answers = iter(["n", "n"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert main([*base, "remove", "Future run", "--path", str(garmin_tree)]) == 0
    assert "removed 0 workouts" in capsys.readouterr().out
    assert main([*base, "remove", "Future run", "missing", "--path", str(garmin_tree), "--yes"]) == 2
    captured = capsys.readouterr()
    assert "removed 2 workouts" in captured.out and "0 of ~25 workouts left" in captured.out and "missing: not found" in captured.err
    assert len(list((tmp_path / "Health" / "workouts" / "watch-backup").iterdir())) >= 1
    assert main([*base, "push", "--upcoming", "--path", str(garmin_tree)]) == 0  # removed -> upcoming again
