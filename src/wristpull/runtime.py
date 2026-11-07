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


def in_container(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return env.get(CONTAINER_ENV, "").strip() == "1"


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
