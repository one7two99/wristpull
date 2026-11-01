"""Minimal JSON-over-HTTP helpers (stdlib only) for the Ollama and OpenRouter backends."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

RETRY_STATUSES = {408, 429, 500, 502, 503, 529}


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str, url: str) -> None:
        super().__init__(f"HTTP {status} from {url}: {body[:300]}")
        self.status = status
        self.body = body
        self.url = url


Opener = Callable[..., Any]  # urllib.request.urlopen signature: (request, timeout=...)


def _request(method: str, url: str, body: dict[str, Any] | None, headers: dict[str, str] | None, timeout: float, opener: Opener) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", "Accept": "application/json", **(headers or {})})
    try:
        with opener(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise HttpError(exc.code, exc.read().decode("utf-8", "replace") if exc.fp else str(exc), url) from exc
    except urllib.error.URLError as exc:
        raise HttpError(0, f"connection failed: {exc.reason}", url) from exc
    except TimeoutError as exc:
        raise HttpError(0, f"timed out after {timeout:g} s", url) from exc
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    try:
        return json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as exc:
        raise HttpError(0, f"invalid JSON: {text[:200]}", url) from exc


def post_json(url: str, body: dict[str, Any], *, headers: dict[str, str] | None = None, timeout: float = 60.0, opener: Opener = urllib.request.urlopen) -> dict[str, Any]:
    return _request("POST", url, body, headers, timeout, opener)


def get_json(url: str, *, headers: dict[str, str] | None = None, timeout: float = 15.0, opener: Opener = urllib.request.urlopen) -> dict[str, Any]:
    return _request("GET", url, None, headers, timeout, opener)


def with_retries(
    fn: Callable[[], Any],
    *,
    attempts: int = 3,
    backoff: tuple[float, float] = (2.0, 8.0),
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = lambda _m: None,
    retry_if: Callable[[HttpError], bool] | None = None,
) -> Any:
    """Call ``fn``; on HttpError with a retryable status (or a connection failure) wait and retry.

    ``retry_if`` can veto a retry for errors that will not go away by waiting.
    """
    delay = backoff[0]
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except HttpError as exc:
            retryable = exc.status in RETRY_STATUSES or exc.status == 0
            if retry_if is not None and not retry_if(exc):
                retryable = False
            if attempt == attempts or not retryable:
                raise
            log(f"  retry {attempt}/{attempts - 1} after {delay:g} s ({exc})")
            sleep(delay)
            delay = min(delay * 2, backoff[1])
    raise RuntimeError("unreachable")
