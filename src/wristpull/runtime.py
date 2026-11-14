"""Where wristpull runs: natively or inside the Docker container (``cli`` service).

The container sets ``WRISTPULL_IN_CONTAINER=1`` and mounts the data directory under
``WRISTPULL_DATA_DIR`` (``/data``); ``WRISTPULL_HOST_DATA_DIR`` names the same directory as the
host sees it, so messages can show paths the user can open on the host. ``WRISTPULL_CONFIG_FILE``
points at the mounted config file for commands run without ``--config``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

CONTAINER_ENV = "WRISTPULL_IN_CONTAINER"
CONFIG_FILE_ENV = "WRISTPULL_CONFIG_FILE"  # the config file when --config is not given (the container mounts it)
DATA_DIR_ENV = "WRISTPULL_DATA_DIR"
STRAP_AUTHKEY_ENV = "WRISTPULL_STRAP_AUTHKEY"  # the band's 16-byte auth key, like the coach API keys: environment, never config
HOST_DATA_DIR_ENV = "WRISTPULL_HOST_DATA_DIR"
HOST_CONFIG_ENV = "WRISTPULL_CONFIG"  # the config file as the host sees it (compose passes .env through)
ENV_FILE_ENV = "WRISTPULL_ENV_FILE"  # the .env file itself, mounted into web and cli for backups (compose: /checkout/.env)
CERTS_DIR_ENV = "WRISTPULL_CERTS_DIR"  # deploy/certs as mounted into web and cli (compose: /certs)
COMPOSE_FILE = "compose.yaml"


def in_container(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return env.get(CONTAINER_ENV, "").strip() == "1"


def env_file(env: Mapping[str, str] | None = None, cwd: Path | None = None) -> Path | None:
    """The ``.env`` next to ``compose.yaml`` for backups: ``WRISTPULL_ENV_FILE``, else ``./.env`` when run from a checkout."""
    env = os.environ if env is None else env
    value = env.get(ENV_FILE_ENV, "").strip()
    if value:
        return Path(value)
    cwd = cwd or Path.cwd()
    return cwd / ".env" if (cwd / COMPOSE_FILE).is_file() else None


def certs_dir(env: Mapping[str, str] | None = None, cwd: Path | None = None) -> Path | None:
    """``deploy/certs`` (own-CA certificate and key) for backups: ``WRISTPULL_CERTS_DIR``, else the checkout's folder."""
    env = os.environ if env is None else env
    value = env.get(CERTS_DIR_ENV, "").strip()
    if value:
        return Path(value)
    cwd = cwd or Path.cwd()
    folder = cwd / "deploy" / "certs"
    return folder if (cwd / COMPOSE_FILE).is_file() and folder.is_dir() else None


def host_path(path: Path, env: Mapping[str, str] | None = None) -> Path:
    """``path`` as the host sees it: the container data directory is replaced by the host one."""
    env = os.environ if env is None else env
    inside, outside = env.get(DATA_DIR_ENV, "").strip(), env.get(HOST_DATA_DIR_ENV, "").strip()
    if not inside or not outside:
        return path
    try:
        return Path(outside) / path.relative_to(inside)
    except ValueError:
        return path
