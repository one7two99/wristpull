"""Secrets and overrides kept in the data directory: ``<data_dir>/secrets.json``, mode 0600.

The dashboard's settings page writes this file (password hash, session secret, the band's auth key,
API keys, the Ollama address); ``.env`` and the shell remain possible sources, but a value stored
here wins (:func:`effective`, :func:`apply`). The file lives next to the database, so every service
that shares the data directory (web, agent, cli) sees the same values, and a backup carries it along
(the archive already holds ``.env``; both are secrets, hence mode 0600).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any

from .config import Config

FILE_NAME = "secrets.json"
PASSWORD_HASH = "WRISTPULL_WEB_PASSWORD_HASH"
WEB_SECRET = "WRISTPULL_WEB_SECRET"
STRAP_AUTHKEY = "WRISTPULL_STRAP_AUTHKEY"
OLLAMA_URL = "WRISTPULL_OLLAMA_URL"
API_KEYS = ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY")
# Environment variables the store may override, in the order the settings page shows them.
KEYS: tuple[str, ...] = (PASSWORD_HASH, WEB_SECRET, STRAP_AUTHKEY, *API_KEYS, OLLAMA_URL)
PASSWORD_MUST_CHANGE = "password_must_change"  # set while the start password is in use
FLAGS: tuple[str, ...] = (PASSWORD_MUST_CHANGE,)


def path(config: Config) -> Path:
    return Path(config.data_dir) / FILE_NAME


def load(config: Config) -> dict[str, str]:
    """The stored values (empty when there is no file). A corrupt file raises ``ValueError``."""
    file = path(config)
    if not file.is_file():
        return {}
    try:
        data = json.loads(file.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{file}: not readable as JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{file}: expected a JSON object")
    return {str(k): str(v) for k, v in data.items() if v not in (None, "")}


def save(config: Config, values: Mapping[str, Any]) -> Path:
    """Write the store atomically with mode 0600; empty values are dropped (that is how a key is cleared)."""
    file = path(config)
    file.parent.mkdir(parents=True, exist_ok=True)
    clean = {str(k): str(v) for k, v in values.items() if v not in (None, "")}
    tmp = file.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(clean, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, file)
    return file


def update(config: Config, changes: Mapping[str, Any]) -> dict[str, str]:
    """Merge ``changes`` into the store (``""``/``None`` removes a key) and return the new contents."""
    values = load(config)
    for key, value in changes.items():
        if value in (None, ""):
            values.pop(key, None)
        else:
            values[key] = str(value)
    save(config, values)
    return values


def effective(name: str, config: Config, env: Mapping[str, str] | None = None) -> str | None:
    """The value of ``name``: the store first, then the environment, else None."""
    env = os.environ if env is None else env
    stored = load(config).get(name)
    if stored:
        return stored
    value = env.get(name, "").strip()
    return value or None


def source_of(name: str, config: Config, env: Mapping[str, str] | None = None) -> str | None:
    """Where a value comes from: ``"store"``, ``"env"`` or None (not set anywhere)."""
    env = os.environ if env is None else env
    if load(config).get(name):
        return "store"
    return "env" if env.get(name, "").strip() else None


def apply(config: Config, env: MutableMapping[str, str] | None = None) -> list[str]:
    """Put every stored key into the environment (overriding what is there), so code that reads
    ``os.environ`` (the strap's auth key, the coach API keys) sees the stored values. Returns the keys set."""
    env = os.environ if env is None else env
    applied: list[str] = []
    for key, value in load(config).items():
        if key in KEYS:
            env[key] = value
            applied.append(key)
    return applied
