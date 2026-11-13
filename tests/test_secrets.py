"""The secrets store in the data directory: mode, precedence over the environment, apply, clearing."""

from pathlib import Path

import pytest

from wristpull import secrets as store
from wristpull.config import Config


def test_store_roundtrip_mode_and_precedence(config: Config, tmp_path: Path):
    assert store.load(config) == {} and store.effective("ANTHROPIC_API_KEY", config, {"ANTHROPIC_API_KEY": "from-env"}) == "from-env"
    assert store.source_of("ANTHROPIC_API_KEY", config, {"ANTHROPIC_API_KEY": "from-env"}) == "env"
    path = store.save(config, {"ANTHROPIC_API_KEY": "stored", "empty": "", "WRISTPULL_STRAP_AUTHKEY": "0x" + "ab" * 16})
    assert path == config.data_dir / "secrets.json" and oct(path.stat().st_mode & 0o777) == "0o600"
    assert store.load(config) == {"ANTHROPIC_API_KEY": "stored", "WRISTPULL_STRAP_AUTHKEY": "0x" + "ab" * 16}
    assert store.effective("ANTHROPIC_API_KEY", config, {"ANTHROPIC_API_KEY": "from-env"}) == "stored"  # the store wins
    assert store.source_of("ANTHROPIC_API_KEY", config, {}) == "store" and store.source_of("OPENROUTER_API_KEY", config, {}) is None
    env: dict[str, str] = {"ANTHROPIC_API_KEY": "from-env", "OTHER": "x"}
    assert store.apply(config, env) == ["ANTHROPIC_API_KEY", "WRISTPULL_STRAP_AUTHKEY"] and env["ANTHROPIC_API_KEY"] == "stored" and env["OTHER"] == "x"
    assert store.update(config, {"ANTHROPIC_API_KEY": None, "OPENROUTER_API_KEY": "or"}) == {"WRISTPULL_STRAP_AUTHKEY": "0x" + "ab" * 16, "OPENROUTER_API_KEY": "or"}
    path.write_text("[not an object]", "utf-8")
    with pytest.raises(ValueError):
        store.load(config)
    path.write_text("{broken", "utf-8")
    with pytest.raises(ValueError):
        store.load(config)


def test_cli_applies_the_store(cfg_file_for_secrets: Path, monkeypatch, capsys):
    """`wristpull status` sees the band's key from the store although the environment has none."""
    import os

    from wristpull.cli import main
    from wristpull.config import load_config

    config = load_config(cfg_file_for_secrets)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    store.save(config, {"WRISTPULL_STRAP_AUTHKEY": "0x" + "cd" * 16})
    monkeypatch.delenv("WRISTPULL_STRAP_AUTHKEY", raising=False)
    cfg_file_for_secrets.write_text(cfg_file_for_secrets.read_text("utf-8") + '[strap]\naddress = "AA:BB:CC:DD:EE:02"\n', "utf-8")
    assert main(["--config", str(cfg_file_for_secrets), "status"]) == 0
    assert "key from the environment" in capsys.readouterr().out and os.environ["WRISTPULL_STRAP_AUTHKEY"].startswith("0xcd")


@pytest.fixture
def cfg_file_for_secrets(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(f'[storage]\ndata_dir = "{(tmp_path / "Health").as_posix()}"\n[time]\ntimezone = "Europe/Berlin"\n', "utf-8")
    return path
