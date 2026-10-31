from pathlib import Path

from wristpull.config import Config, load_config, write_default_config


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
