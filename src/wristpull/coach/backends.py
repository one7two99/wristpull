"""LLM backends behind one small interface. Wave C1: Ollama (local) and a fake for tests."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import Config
from ._http import HttpError, Opener, get_json, post_json, with_retries

Log = Callable[[str], None]


class CoachError(RuntimeError):
    """A problem talking to the model, with a message meant for the user."""


@dataclass
class ChatRequest:
    system: str
    user: str
    model: str
    max_tokens: int = 2000
    temperature: float | None = None


@dataclass
class ChatResponse:
    text: str
    model: str
    backend: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    tokens_estimated: bool = False
    stop_reason: str | None = None
    cost_usd: float | None = None
    duration_s: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def truncated(self) -> bool:
        return self.stop_reason in ("length", "max_tokens")


class Backend(Protocol):
    name: str
    model: str

    def complete(self, request: ChatRequest, *, log: Log = lambda _m: None) -> ChatResponse: ...

    def check(self) -> str:
        """Human-readable availability check; raises CoachError when unusable."""
        ...


def estimate_tokens(text: str) -> int:
    """Rough token estimate for budgeting (English/German prose and Markdown tables: ~3.6 chars/token)."""
    return max(1, int(len(text) / 3.6))


class FakeBackend:
    """Returns canned answers and records requests; used in tests and for --dry-run previews."""

    name = "fake"

    def __init__(self, replies: list[str] | None = None, *, model: str = "fake-model", fail: Exception | None = None) -> None:
        self.replies = list(replies or ["## Status\nok\n"])
        self.model = model
        self.requests: list[ChatRequest] = []
        self.fail = fail

    def complete(self, request: ChatRequest, *, log: Log = lambda _m: None) -> ChatResponse:
        self.requests.append(request)
        if self.fail is not None:
            raise self.fail
        text = self.replies[min(len(self.requests) - 1, len(self.replies) - 1)]
        return ChatResponse(text=text, model=self.model, backend=self.name, input_tokens=estimate_tokens(request.system + request.user), output_tokens=estimate_tokens(text), tokens_estimated=True, stop_reason="end_turn")

    def check(self) -> str:
        return "fake backend ready"


class OllamaBackend:
    """Local Ollama server, POST /api/chat without streaming."""

    name = "ollama"

    def __init__(self, url: str, model: str, *, timeout_s: float = 600.0, num_ctx: int = 8192, opener: Opener | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.num_ctx = num_ctx
        self._opener = opener
        self._sleep = sleep

    def _kwargs(self) -> dict[str, Any]:
        return {"opener": self._opener} if self._opener is not None else {}

    def check(self) -> str:
        try:
            tags = get_json(f"{self.url}/api/tags", **self._kwargs())
        except HttpError as exc:
            raise CoachError(f"Ollama not reachable at {self.url} ({exc})") from exc
        names = {m.get("name", "") for m in tags.get("models", [])}
        if self.model not in names and f"{self.model}:latest" not in names:
            raise CoachError(f"model {self.model!r} is not pulled in Ollama; available: {', '.join(sorted(names)) or 'none'}")
        return f"ollama {self.model} ready"

    def complete(self, request: ChatRequest, *, log: Log = lambda _m: None) -> ChatResponse:
        body: dict[str, Any] = {
            "model": request.model or self.model,
            "stream": False,
            "messages": [{"role": "system", "content": request.system}, {"role": "user", "content": request.user}],
            "options": {"num_predict": request.max_tokens, "num_ctx": self.num_ctx},
        }
        if request.temperature is not None:
            body["options"]["temperature"] = request.temperature
        started = time.monotonic()
        try:
            data = with_retries(
                lambda: post_json(f"{self.url}/api/chat", body, timeout=self.timeout_s, **self._kwargs()),
                sleep=self._sleep,
                log=log,
                retry_if=lambda exc: "system memory" not in exc.body,
            )
        except HttpError as exc:
            if exc.status == 0:
                raise CoachError(f"Ollama not reachable at {self.url} ({exc.body})") from exc
            if exc.status == 404:
                raise CoachError(f"Ollama: model {body['model']!r} not found (ollama pull {body['model']})") from exc
            if "system memory" in exc.body:
                raise CoachError(
                    f"Ollama: not enough free RAM for {body['model']} with a {self.num_ctx}-token context "
                    f"({exc.body.strip()[:160]}); close other programs, choose a smaller model or lower [coach] num_ctx"
                ) from exc
            raise CoachError(f"Ollama error: {exc}") from exc
        message = data.get("message") or {}
        text = message.get("content") or ""
        if not text:
            raise CoachError("Ollama returned an empty answer")
        return ChatResponse(
            text=text,
            model=data.get("model") or body["model"],
            backend=self.name,
            input_tokens=data.get("prompt_eval_count"),
            output_tokens=data.get("eval_count"),
            tokens_estimated=data.get("prompt_eval_count") is None,
            stop_reason=data.get("done_reason"),
            cost_usd=0.0,
            duration_s=time.monotonic() - started,
            raw={k: v for k, v in data.items() if k != "message"},
        )


def make_backend(config: Config, *, model: str | None = None, backend: str | None = None) -> Backend:
    kind = backend or config.coach.backend
    chosen = model or config.coach.model
    if kind == "ollama":
        return OllamaBackend(config.coach.ollama_url, chosen, timeout_s=config.coach.timeout_s, num_ctx=config.coach.num_ctx)
    if kind in ("claude", "openrouter"):
        raise CoachError(f"backend {kind!r} comes in the next coach wave; use ollama for now")
    raise CoachError(f"unknown coach backend {kind!r}")
