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
