"""LLM backends behind one small interface. Wave C1: Ollama (local) and a fake for tests."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import Config
from ._http import HttpError, Opener, get_json, post_json, with_retries

OPENROUTER_URL = "https://openrouter.ai/api/v1"
DEFAULT_KEY_ENV = {"claude": "ANTHROPIC_API_KEY", "openrouter": "OPENROUTER_API_KEY"}
# Models that accept the server-side refusal fallback ("default" form) on the Claude API.
CLAUDE_FALLBACK_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-opus-5")

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


class ClaudeBackend:
    """Anthropic Messages API through the official SDK (optional extra: uv sync --extra coach)."""

    name = "claude"

    def __init__(self, model: str, *, api_key: str | None = None, effort: str = "medium", timeout_s: float = 120.0, client: Any | None = None) -> None:
        self.model = model
        self.effort = effort
        self._client = client
        self._api_key = api_key
        self._timeout = timeout_s

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on the environment
                raise CoachError("the Claude backend needs the optional dependency: uv sync --extra coach") from exc
            kwargs: dict[str, Any] = {"timeout": self._timeout, "max_retries": 2}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

    def check(self) -> str:
        client = self._get_client()
        try:
            client.models.retrieve(self.model)
        except Exception as exc:  # noqa: BLE001 - mapped below
            raise self._map(exc) from exc
        return f"claude {self.model} ready"

    @staticmethod
    def _map(exc: Exception) -> CoachError:
        try:
            import anthropic
        except ImportError:  # pragma: no cover
            return CoachError(str(exc))
        if isinstance(exc, anthropic.AuthenticationError):
            return CoachError("Claude: API key missing or invalid (set ANTHROPIC_API_KEY or [coach] api_key_env, or run `ant auth login`)")
        if isinstance(exc, anthropic.NotFoundError):
            return CoachError(f"Claude: {exc.message}")
        if isinstance(exc, anthropic.RateLimitError):
            return CoachError("Claude: rate limited, try again in a minute")
        if isinstance(exc, anthropic.APIStatusError):
            return CoachError(f"Claude: API error {exc.status_code}: {exc.message}")
        if isinstance(exc, anthropic.APIConnectionError):
            return CoachError(f"Claude: connection problem ({exc})")
        return CoachError(f"Claude: {exc}")

    def complete(self, request: ChatRequest, *, log: Log = lambda _m: None) -> ChatResponse:
        client = self._get_client()
        model = request.model or self.model
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": request.user}],
            "output_config": {"effort": self.effort},
        }
        started = time.monotonic()
        try:
            if any(model.startswith(prefix) for prefix in CLAUDE_FALLBACK_MODELS):
                # a safety-classifier refusal is re-run on a fallback model inside the same call
                response = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                response = client.messages.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - mapped to a user-facing error
            raise self._map(exc) from exc
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            raise CoachError("Claude declined the request" + (f" ({details.category})" if details and getattr(details, "category", None) else ""))
        text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        if not text.strip():
            raise CoachError("Claude returned an empty answer")
        usage = getattr(response, "usage", None)
        return ChatResponse(
            text=text,
            model=getattr(response, "model", model),
            backend=self.name,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            stop_reason=getattr(response, "stop_reason", None),
            duration_s=time.monotonic() - started,
        )


class OpenRouterBackend:
    """OpenRouter's OpenAI-compatible chat completions endpoint."""

    name = "openrouter"

    def __init__(self, model: str, *, api_key: str, url: str = OPENROUTER_URL, timeout_s: float = 120.0, opener: Opener | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self.model = model
        self.url = url.rstrip("/")
        self._key = api_key
        self.timeout_s = timeout_s
        self._opener = opener
        self._sleep = sleep

    def _kwargs(self) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._key}", "HTTP-Referer": "https://github.com/one7two99/wristpull", "X-Title": "wristpull"}
        out: dict[str, Any] = {"headers": headers}
        if self._opener is not None:
            out["opener"] = self._opener
        return out

    def check(self) -> str:
        try:
            info = get_json(f"{self.url}/key", **self._kwargs())
        except HttpError as exc:
            if exc.status == 401:
                raise CoachError("OpenRouter: API key invalid (set OPENROUTER_API_KEY or [coach] api_key_env)") from exc
            raise CoachError(f"OpenRouter not reachable ({exc})") from exc
        label = (info.get("data") or {}).get("label") or "key"
        return f"openrouter {self.model} ready ({label})"

    def complete(self, request: ChatRequest, *, log: Log = lambda _m: None) -> ChatResponse:
        body: dict[str, Any] = {
            "model": request.model or self.model,
            "messages": [{"role": "system", "content": request.system}, {"role": "user", "content": request.user}],
            "max_tokens": request.max_tokens,
            "usage": {"include": True},
        }
        if request.temperature is not None:
            body["temperature"] = request.temperature
        started = time.monotonic()
        try:
            data = with_retries(lambda: post_json(f"{self.url}/chat/completions", body, timeout=self.timeout_s, **self._kwargs()), sleep=self._sleep, log=log)
        except HttpError as exc:
            if exc.status == 401:
                raise CoachError("OpenRouter: API key invalid (set OPENROUTER_API_KEY or [coach] api_key_env)") from exc
            if exc.status in (402,):
                raise CoachError("OpenRouter: insufficient credits") from exc
            if exc.status == 404:
                raise CoachError(f"OpenRouter: model {body['model']!r} not found") from exc
            raise CoachError(f"OpenRouter error: {exc}") from exc
        if "error" in data and not data.get("choices"):
            raise CoachError(f"OpenRouter error: {data['error'].get('message', data['error']) if isinstance(data['error'], dict) else data['error']}")
        choices = data.get("choices") or []
        text = ((choices[0].get("message") or {}).get("content") or "") if choices else ""
        if not text.strip():
            raise CoachError("OpenRouter returned an empty answer")
        usage = data.get("usage") or {}
        return ChatResponse(
            text=text,
            model=data.get("model") or body["model"],
            backend=self.name,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            tokens_estimated=usage.get("prompt_tokens") is None,
            stop_reason=(choices[0].get("finish_reason") if choices else None),
            cost_usd=usage.get("cost"),
            duration_s=time.monotonic() - started,
            raw={k: v for k, v in data.items() if k != "choices"},
        )


def api_key_for(config: Config, backend: str, env: Callable[[str], str | None] = os.environ.get) -> str | None:
    """The API key from the configured environment variable (or the backend's default variable)."""
    name = config.coach.api_key_env or DEFAULT_KEY_ENV.get(backend)
    return env(name) if name else None


def make_backend(config: Config, *, model: str | None = None, backend: str | None = None, kind: str = "daily") -> Backend:
    """Build the configured backend; the weekly review may use [coach] weekly_model."""
    name = backend or config.coach.backend
    chosen = model or (config.coach.weekly_model if kind == "weekly" and config.coach.weekly_model else config.coach.model)
    if name == "ollama":
        return OllamaBackend(config.coach.ollama_url, chosen, timeout_s=config.coach.timeout_s, num_ctx=config.coach.num_ctx)
    if name == "claude":
        return ClaudeBackend(chosen, api_key=api_key_for(config, "claude"), effort=config.coach.effort, timeout_s=min(config.coach.timeout_s, 300.0))
    if name == "openrouter":
        key = api_key_for(config, "openrouter")
        if not key:
            raise CoachError(f"OpenRouter needs an API key in ${config.coach.api_key_env or DEFAULT_KEY_ENV['openrouter']}")
        return OpenRouterBackend(chosen, api_key=key, timeout_s=min(config.coach.timeout_s, 300.0))
    raise CoachError(f"unknown coach backend {name!r}")
