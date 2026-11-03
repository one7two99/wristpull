"""Login for the web dashboard: scrypt password hashes, signed session cookies, CSRF tokens, rate limit.

Only the password *hash* and the session secret come from the environment
(``WRISTPULL_WEB_PASSWORD_HASH``, ``WRISTPULL_WEB_SECRET``); nothing secret lives in config files.
Everything here is stdlib, so the web extra stays small.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# scrypt cost: 2**14 needs 16 MiB and about 50 ms, the usual interactive setting.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
SESSION_DAYS = 30


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, *, salt: bytes | None = None, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> str:
    """``scrypt:<n>:<r>:<p>:<salt>:<hash>`` (colons, not ``$``, so the value survives Compose's ``.env`` interpolation)."""
    if not password:
        raise ValueError("empty password")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt:{n}:{r}:{p}:{_b64(salt)}:{_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = encoded.strip().split(":")
        if scheme != "scrypt":
            return False
        expected = _unb64(digest)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def valid_hash(encoded: str | None) -> bool:
    """A well-formed scrypt hash (checked at start-up, so a typo in ``.env`` fails loudly)."""
    if not encoded:
        return False
    parts = encoded.strip().split(":")
    return len(parts) == 6 and parts[0] == "scrypt" and all(x.isdigit() for x in parts[1:4])


class SessionCodec:
    """A session as signed JSON in a cookie: ``<payload>.<hmac>``; nothing is stored on the server."""

    def __init__(self, secret: str, *, max_age_s: int = SESSION_DAYS * 86400, clock: Callable[[], float] = time.time):
        if len(secret) < 16:
            raise ValueError("the session secret must be at least 16 characters")
        self._key = hashlib.sha256(("wristpull-session:" + secret).encode("utf-8")).digest()
        self.max_age_s = max_age_s
        self._clock = clock

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self._key, payload.encode("ascii"), hashlib.sha256).digest())

    def dumps(self, data: dict[str, Any]) -> str:
        payload = _b64(json.dumps({**data, "iat": int(self._clock())}, separators=(",", ":")).encode("utf-8"))
        return f"{payload}.{self._sign(payload)}"

    def loads(self, cookie: str | None) -> dict[str, Any] | None:
        if not cookie or "." not in cookie:
            return None
        payload, signature = cookie.rsplit(".", 1)
        if not hmac.compare_digest(signature, self._sign(payload)):
            return None
        try:
            data = json.loads(_unb64(payload))
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict) or self._clock() - float(data.get("iat", 0)) > self.max_age_s:
            return None
        return data


def new_session(*, user: bool = False) -> dict[str, Any]:
    """A fresh session: a new CSRF token, logged in or not."""
    return {"csrf": secrets.token_urlsafe(24), "user": user}


@dataclass
class RateLimiter:
    """Failed logins per client within a window, plus a global cap so changing addresses does not help.

    Lives in process memory: the reason the server runs exactly one worker.
    """

    per_client: int = 5
    overall: int = 20
    window_s: float = 15 * 60
    clock: Callable[[], float] = time.monotonic
    _failures: dict[str, list[float]] = field(default_factory=dict)

    def _recent(self, key: str) -> list[float]:
        now = self.clock()
        kept = [t for t in self._failures.get(key, []) if now - t < self.window_s]
        self._failures[key] = kept
        return kept

    def blocked(self, client: str) -> bool:
        return len(self._recent(client)) >= self.per_client or len(self._recent("*")) >= self.overall

    def failure(self, client: str) -> None:
        now = self.clock()
        self._failures.setdefault(client, []).append(now)
        self._failures.setdefault("*", []).append(now)

    def success(self, client: str) -> None:
        self._failures.pop(client, None)
