from pathlib import Path

import pytest

from wristpull.config import Config, load_config, validate_config, write_config, write_default_config


def test_defaults_without_file(tmp_path: Path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.path is None
    assert cfg.db_path == cfg.data_dir / "wristpull.sqlite"
    assert cfg.raw_dir == cfg.data_dir / "raw"
    assert "Monitor" in cfg.garmin_folders


def test_write_and_load_roundtrip(tmp_path: Path):
    path = tmp_path / "cfg" / "config.toml"
    write_default_config(path)
    cfg = load_config(path)
    assert cfg.path == path
    assert cfg.timezone == Config().timezone


def test_load_custom_values(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[storage]\ndata_dir = "~/Somewhere/health"\n[time]\ntimezone = "Europe/Vienna"\n'
        '[garmin]\nfolders = ["Activity"]\nmount_hints = ["/mnt/watch"]\n[phone]\nserial = "ABC"\n',
        "utf-8",
    )
    cfg = load_config(path)
    assert cfg.data_dir == Path.home() / "Somewhere" / "health"
    assert cfg.timezone == "Europe/Vienna"
    assert cfg.garmin_folders == ["Activity"]
    assert cfg.garmin_mount_hints == [Path("/mnt/watch")]
    assert cfg.phone_serial == "ABC"
    rendered = cfg.render()
    assert 'timezone = "Europe/Vienna"' in rendered
    assert 'data_dir = "~/Somewhere/health"' in rendered


def test_ui_section_defaults_and_values(tmp_path: Path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.ui_language is None and cfg.ui_theme == "textual-dark" and cfg.ui_range_days == 28
    path = tmp_path / "config.toml"
    path.write_text('[ui]\nlanguage = "de"\ntheme = "nord"\nrange_days = 0\n', "utf-8")
    cfg = load_config(path)
    assert cfg.ui_language == "de" and cfg.ui_theme == "nord" and cfg.ui_range_days == 1
    rendered = cfg.render()
    assert '[ui]' in rendered and 'language = "de"' in rendered and "range_days = 1" in rendered


def test_validate_and_write_config_roundtrip(tmp_path: Path):
    cfg = Config(data_dir=tmp_path / "H", timezone="Europe/Berlin", garmin_mount_hints=[Path("/mnt/watch")], phone_serial="ABC", ui_language="de", ui_range_days=14)
    assert validate_config(cfg) == []
    path = write_config(cfg, tmp_path / "cfg" / "config.toml")
    assert cfg.path == path
    loaded = load_config(path)
    assert loaded.garmin_mount_hints == [Path("/mnt/watch")] and loaded.phone_serial == "ABC"
    assert loaded.ui_language == "de" and loaded.ui_range_days == 14 and loaded.data_dir == tmp_path / "H"
    bad = Config(timezone="Mars/Olympus", garmin_folders=[], ui_language="xx", ui_range_days=0, phone_export_dir="relative")
    problems = validate_config(bad)
    assert len(problems) == 5 and any("timezone" in p for p in problems)
    with pytest.raises(ValueError):
        write_config(bad, tmp_path / "bad.toml")
    assert not (tmp_path / "bad.toml").exists()
