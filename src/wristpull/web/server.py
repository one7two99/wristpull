"""``wristpull serve``: read the secrets from the environment and run uvicorn with exactly one worker."""

from __future__ import annotations

import os
import secrets
import sys
from collections.abc import Callable
from dataclasses import replace
from getpass import getpass
from pathlib import Path
from typing import Any

from ..config import Config
from .auth import hash_password, valid_hash

PASSWORD_ENV = "WRISTPULL_WEB_PASSWORD_HASH"
SECRET_ENV = "WRISTPULL_WEB_SECRET"


def prompt_hash(ask: Callable[[str], str] | None = None) -> str | None:
    """Ask twice for a password and return its hash (None when the two differ or it is empty)."""
    ask = ask or getpass
    first = ask("Dashboard password: ")
    if not first or first != ask("Repeat: "):
        return None
    return hash_password(first)


def resolve_secret(env: dict[str, str], warn: Callable[[str], None]) -> str:
    """``WRISTPULL_WEB_SECRET``, or a random one per start (sessions then end when the process stops)."""
    secret = env.get(SECRET_ENV, "").strip()
    if secret:
        return secret
    warn(f"{SECRET_ENV} is not set: using a random secret, so every login ends when the server stops. "
         "Set it to keep sessions across restarts (python3 -c \"import secrets; print(secrets.token_urlsafe(32))\").")
    return secrets.token_urlsafe(32)


def serve(
    config: Config,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    data_dir: Path | None = None,
    behind_proxy: bool = False,
    env: dict[str, str] | None = None,
    run: Callable[..., Any] | None = None,
    warn: Callable[[str], None] = lambda m: print(m, file=sys.stderr),
) -> int:
    env = dict(os.environ) if env is None else env
    password_hash = env.get(PASSWORD_ENV, "").strip()
    if not valid_hash(password_hash):
        warn(f"{PASSWORD_ENV} is missing or malformed. Create one with: wristpull serve --hash-password")
        return 1
    secret = resolve_secret(env, warn)
    if data_dir is not None:
        config = replace(config, data_dir=data_dir)
    from .app import create_app

    app = create_app(config, password_hash=password_hash, secret=secret)
    if run is None:
        import uvicorn

        run = uvicorn.run
    # Exactly one worker: the login rate limit and the job registry live in this process's memory.
    # Do not add a --workers option; a second worker would not see either.
    run(
        app,
        host=host,
        port=port,
        workers=1,
        proxy_headers=behind_proxy,
        forwarded_allow_ips="*" if behind_proxy else None,
        server_header=False,
        log_level="info",
    )
    return 0
