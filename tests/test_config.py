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
        '[garmin]\nfolders = ["Activity"]\n[phone]\nserial = "ABC"\n',
        "utf-8",
    )
    cfg = load_config(path)
    assert cfg.data_dir == Path.home() / "Somewhere" / "health"
    assert cfg.timezone == "Europe/Vienna"
    assert cfg.garmin_folders == ["Activity"]
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
    cfg = Config(data_dir=tmp_path / "H", timezone="Europe/Berlin", phone_serial="ABC", ui_language="de", ui_range_days=14)
    assert validate_config(cfg) == []
    path = write_config(cfg, tmp_path / "cfg" / "config.toml")
    assert cfg.path == path
    loaded = load_config(path)
    assert loaded.phone_serial == "ABC"
    assert loaded.ui_language == "de" and loaded.ui_range_days == 14 and loaded.data_dir == tmp_path / "H"
    bad = Config(timezone="Mars/Olympus", garmin_folders=[], ui_language="xx", ui_range_days=0, phone_export_dir="relative")
    problems = validate_config(bad)
    assert len(problems) == 5 and any("timezone" in p for p in problems)
    with pytest.raises(ValueError):
        write_config(bad, tmp_path / "bad.toml")
    assert not (tmp_path / "bad.toml").exists()


def test_coach_section_roundtrip_and_validation(tmp_path: Path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.coach.backend == "ollama" and cfg.coach.model == "gemma3:4b" and cfg.coach.price_for("claude-opus-5-5") == (4.0, 20.0)
    path = tmp_path / "config.toml"
    path.write_text(
        '[coach]\nbackend = "claude"\nmodel = "claude-sonnet-5-5"\nweekly_model = "claude-opus-5-5"\napi_key_env = "MY_KEY"\n'
        'max_tokens = 1500\ntimeout_s = 90\ntoken_budget = 12000\neffort = "high"\nlanguage = "de"\n'
        '[coach.prices]\n"my-model" = { input = 1.5, output = 3 }\n',
        "utf-8",
    )
    cfg = load_config(path)
    c = cfg.coach
    assert (c.backend, c.model, c.weekly_model, c.api_key_env, c.max_tokens, c.timeout_s, c.token_budget, c.effort, c.language) == ("claude", "claude-sonnet-5-5", "claude-opus-5-5", "MY_KEY", 1500, 90.0, 12000, "high", "de")
    assert c.price_for("my-model") == (1.5, 3.0) and c.price_for("nothing") is None
    rendered = cfg.render()
    assert 'backend = "claude"' in rendered and '"my-model" = { input = 1.5, output = 3 }' in rendered
    assert load_config(write_config(cfg, tmp_path / "again.toml")).coach.prices == {"my-model": (1.5, 3.0)}
    assert validate_config(cfg) == []
    cfg.coach.backend, cfg.coach.model, cfg.coach.max_tokens, cfg.coach.effort, cfg.coach.language = "bogus", " ", 10, "ultra", "fr"
    problems = validate_config(cfg)
    assert len(problems) == 5 and any("backend" in p for p in problems)


def test_data_dir_env_override_keeps_the_file_value(tmp_path: Path):
    path = tmp_path / "cfg" / "config.toml"
    write_default_config(path)
    env = {"WRISTPULL_DATA_DIR": "/data"}
    cfg = load_config(path, env=env)
    assert cfg.data_dir == Path("/data")
    assert cfg.file_data_dir == Config().data_dir
    assert cfg.db_path == Path("/data/wristpull.sqlite")
    # writing the config back keeps the host path, not the container mount
    assert 'data_dir = "/data"' not in cfg.render()
    assert load_config(path, env={}).file_data_dir is None
    assert load_config(tmp_path / "nowhere.toml", env=env).data_dir == Path("/data")


def test_config_file_env_names_the_default_path(tmp_path: Path):
    from wristpull.config import default_config_path

    target = tmp_path / "mounted.toml"
    write_default_config(target)
    assert default_config_path({"WRISTPULL_CONFIG_FILE": str(target)}) == target
    assert default_config_path({"XDG_CONFIG_HOME": "/x"}) == Path("/x/wristpull/config.toml")
    assert load_config(env={"WRISTPULL_CONFIG_FILE": str(target)}).path == target


def test_toml_path_uses_tilde_and_forward_slashes(monkeypatch):
    from wristpull.config import toml_path

    home = Path.home()
    assert toml_path(home / "Health" / "wristpull") == "~/Health/wristpull"
    assert toml_path(home) == "~"
    assert toml_path(Path("/srv/wp")) == "/srv/wp"
    assert toml_path(Path(str(home) + "sibling")) == (str(home) + "sibling").replace("\\", "/")  # a prefix, not the home itself
    assert toml_path(Path(r"C:\Users\x\Health")) == "C:/Users/x/Health"  # Windows: forward slashes keep the TOML valid
