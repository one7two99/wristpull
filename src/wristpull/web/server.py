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

from .. import secrets as store
from ..config import Config
from .auth import hash_password, valid_hash

PASSWORD_ENV = store.PASSWORD_HASH
SECRET_ENV = store.WEB_SECRET
OLLAMA_ENV = store.OLLAMA_URL  # the container reaches Ollama on the host under another address
INITIAL_PASSWORD = "wristpull"  # accepted only while no password hash exists anywhere; must be changed at first login


def prompt_hash(ask: Callable[[str], str] | None = None) -> str | None:
    """Ask twice for a password and return its hash (None when the two differ or it is empty)."""
    ask = ask or getpass
    first = ask("Dashboard password: ")
    if not first or first != ask("Repeat: "):
        return None
    return hash_password(first)


def resolve_secret(env: dict[str, str], warn: Callable[[str], None], config: Config | None = None) -> str:
    """The session secret: the store, then ``WRISTPULL_WEB_SECRET``; without either a random one is generated
    and, when a config is given, saved in the store so logins survive restarts."""
    if config is not None:
        stored = store.load(config).get(SECRET_ENV, "").strip()
        if stored:
            return stored
    secret = env.get(SECRET_ENV, "").strip()
    if secret:
        return secret
    secret = secrets.token_urlsafe(32)
    if config is not None:
        store.update(config, {SECRET_ENV: secret})
        warn(f"{SECRET_ENV} was not set: generated one and saved it in {store.path(config)} (sessions survive restarts).")
    else:
        warn(f"{SECRET_ENV} is not set: using a random secret, so every login ends when the server stops. "
             "Set it to keep sessions across restarts (python3 -c \"import secrets; print(secrets.token_urlsafe(32))\").")
    return secret


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
    if data_dir is not None:
        config = replace(config, data_dir=data_dir)
    password_hash = env.get(PASSWORD_ENV, "").strip()
    if password_hash and not valid_hash(password_hash):
        warn(f"{PASSWORD_ENV} is malformed. Create one with: wristpull serve --hash-password")
        return 1
    try:
        stored = store.load(config)
    except ValueError as exc:
        warn(str(exc))
        return 1
    if not stored.get(PASSWORD_ENV) and not password_hash:
        warn(f"No dashboard password yet: log in with the start password '{INITIAL_PASSWORD}' and change it right away "
             "(the login leads to the settings page). Or set WRISTPULL_WEB_PASSWORD_HASH: wristpull serve --hash-password")
    secret = resolve_secret(env, warn, config)
    ollama_url = stored.get(OLLAMA_ENV, "").strip() or env.get(OLLAMA_ENV, "").strip()
    if ollama_url:
        config = replace(config, coach=replace(config.coach, ollama_url=ollama_url))
    store.apply(config)  # stored API keys and the band's key override the environment for coach runs
    from .app import create_app

    app = create_app(config, password_hash=password_hash or None, secret=secret)
    app.state.config = config  # what the app actually runs with (tests read it)
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
