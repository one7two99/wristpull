import io
import json
import urllib.error

import pytest

from wristpull.coach._http import HttpError, get_json, post_json, with_retries
from wristpull.coach.backends import ChatRequest, ClaudeBackend, CoachError, FakeBackend, OllamaBackend, OpenRouterBackend, api_key_for, estimate_tokens, make_backend
from wristpull.config import Config


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def opener_with(responses: list):
    """An urlopen stand-in; each entry is a dict (JSON reply), an int (HTTP error status) or an Exception."""
    calls: list = []

    def opener(req, timeout=None):
        calls.append((req.get_method(), req.full_url, json.loads(req.data) if req.data else None, timeout))
        entry = responses.pop(0)
        if isinstance(entry, int):
            raise urllib.error.HTTPError(req.full_url, entry, "err", {}, io.BytesIO(b'{"error":"boom"}'))
        if isinstance(entry, Exception):
            raise entry
        return FakeResponse(json.dumps(entry).encode())

    opener.calls = calls
    return opener


def test_post_and_get_json_and_errors():
    opener = opener_with([{"ok": 1}, 404, urllib.error.URLError("refused")])
    assert post_json("http://x/api", {"a": 1}, opener=opener) == {"ok": 1}
    assert opener.calls[0][0] == "POST" and opener.calls[0][2] == {"a": 1}
    with pytest.raises(HttpError) as exc:
        get_json("http://x/api", opener=opener)
    assert exc.value.status == 404 and "boom" in exc.value.body
    with pytest.raises(HttpError) as exc:
        get_json("http://x/api", opener=opener)
    assert exc.value.status == 0 and "connection failed" in exc.value.body


def test_with_retries_retries_only_retryable_statuses():
    attempts = iter([HttpError(503, "busy", "u"), HttpError(429, "slow", "u"), {"done": True}])
    slept: list[float] = []
    logs: list[str] = []
    result = with_retries(lambda: (lambda e: (_ for _ in ()).throw(e) if isinstance(e, Exception) else e)(next(attempts)), sleep=slept.append, log=logs.append)
    assert result == {"done": True} and slept == [2.0, 4.0] and len(logs) == 2
    with pytest.raises(HttpError):
        with_retries(lambda: (_ for _ in ()).throw(HttpError(400, "bad", "u")), sleep=slept.append)
    assert len(slept) == 2  # no sleep for a 400


def test_ollama_backend_complete_and_check():
    opener = opener_with([
        {"models": [{"name": "gemma3:4b"}]},
        {"model": "gemma3:4b", "message": {"role": "assistant", "content": "## Status\nfine"}, "prompt_eval_count": 1200, "eval_count": 80, "done_reason": "stop"},
    ])
    backend = OllamaBackend("http://localhost:11434/", "gemma3:4b", timeout_s=5, opener=opener, sleep=lambda _s: None)
    assert backend.check() == "ollama gemma3:4b ready"
    response = backend.complete(ChatRequest(system="sys", user="usr", model="gemma3:4b", max_tokens=500))
    assert response.text.startswith("## Status") and response.input_tokens == 1200 and response.output_tokens == 80
    assert response.stop_reason == "stop" and not response.truncated and response.cost_usd == 0.0
    method, url, body, timeout = opener.calls[1]
    assert url == "http://localhost:11434/api/chat" and body["stream"] is False and body["options"]["num_ctx"] == 8192
    assert body["options"]["num_predict"] == 500 and body["messages"][0]["role"] == "system" and timeout == 5


def test_ollama_backend_errors():
    backend = OllamaBackend("http://localhost:11434", "gemma3:4b", opener=opener_with([{"models": [{"name": "other:latest"}]}]), sleep=lambda _s: None)
    with pytest.raises(CoachError, match="not pulled"):
        backend.check()
    backend = OllamaBackend("http://localhost:11434", "gemma3:4b", opener=opener_with([404]), sleep=lambda _s: None)
    with pytest.raises(CoachError, match="not found"):
        backend.complete(ChatRequest("s", "u", "gemma3:4b"))
    backend = OllamaBackend("http://localhost:11434", "gemma3:4b", opener=opener_with([urllib.error.URLError("refused")] * 3), sleep=lambda _s: None)
    with pytest.raises(CoachError, match="not reachable"):
        backend.complete(ChatRequest("s", "u", "gemma3:4b"))
    backend = OllamaBackend("http://localhost:11434", "gemma3:4b", opener=opener_with([{"message": {"content": ""}}]), sleep=lambda _s: None)
    with pytest.raises(CoachError, match="empty"):
        backend.complete(ChatRequest("s", "u", "gemma3:4b"))


def test_fake_backend_and_factory():
    fake = FakeBackend(["a", "b"])
    assert fake.complete(ChatRequest("s", "u", "m")).text == "a"
    assert fake.complete(ChatRequest("s", "u", "m")).text == "b"
    assert fake.complete(ChatRequest("s", "u", "m")).text == "b" and len(fake.requests) == 3
    assert fake.check() == "fake backend ready"
    with pytest.raises(RuntimeError):
        FakeBackend(fail=RuntimeError("x")).complete(ChatRequest("s", "u", "m"))
    assert estimate_tokens("") == 1 and 25 <= estimate_tokens("a" * 100) <= 30
    config = Config()
    backend = make_backend(config)
    assert isinstance(backend, OllamaBackend) and backend.model == "gemma3:4b"
    assert make_backend(config, model="gemma3:12b", backend="ollama").model == "gemma3:12b"
    with pytest.raises(CoachError, match="unknown"):
        make_backend(config, backend="nope")


def test_ollama_memory_error_is_not_retried():
    body = '{"error":"model requires more system memory (4.1 GiB) than is available (3.7 GiB)"}'

    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "err", {}, io.BytesIO(body.encode()))

    slept: list[float] = []
    backend = OllamaBackend("http://localhost:11434", "gemma3:4b", num_ctx=4096, opener=opener, sleep=slept.append)
    with pytest.raises(CoachError, match="not enough free RAM .* 4096-token"):
        backend.complete(ChatRequest("s", "u", "gemma3:4b"))
    assert slept == []
    assert make_backend(Config()).num_ctx == 8192


class _Block:
    def __init__(self, text, type="text"):
        self.text, self.type = text, type


class _Usage:
    input_tokens, output_tokens = 1500, 300


class _Response:
    def __init__(self, text="## Status\nok", stop_reason="end_turn"):
        self.content = [_Block("ignored", "thinking"), _Block(text)]
        self.usage = _Usage()
        self.stop_reason = stop_reason
        self.model = "claude-opus-5-5"
        self.stop_details = None


class _Messages:
    def __init__(self, response, fail=None):
        self.response, self.fail, self.calls = response, fail, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise self.fail
        return self.response


class _FakeClaudeClient:
    def __init__(self, response=None, fail=None):
        self.messages = _Messages(response or _Response(), fail)
        self.beta = type("Beta", (), {})()
        self.beta.messages = _Messages(response or _Response(), fail)
        self.models = type("Models", (), {"retrieve": lambda self_, model: {"id": model}})()


def test_claude_backend_uses_fallbacks_for_current_models():
    client = _FakeClaudeClient()
    backend = ClaudeBackend("claude-opus-5-5", effort="high", client=client)
    assert backend.check() == "claude claude-opus-5-5 ready"
    response = backend.complete(ChatRequest(system="sys", user="usr", model="claude-opus-5-5", max_tokens=700))
    assert response.text == "## Status\nok" and response.input_tokens == 1500 and response.output_tokens == 300
    assert client.messages.calls == [] and len(client.beta.messages.calls) == 1
    call = client.beta.messages.calls[0]
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["output_config"] == {"effort": "high"} and call["max_tokens"] == 700 and call["system"] == "sys"
    assert "temperature" not in call and call["messages"] == [{"role": "user", "content": "usr"}]
    # an older model goes through the plain endpoint
    client = _FakeClaudeClient()
    ClaudeBackend("claude-haiku-4-5", client=client).complete(ChatRequest("s", "u", "claude-haiku-4-5"))
    assert len(client.messages.calls) == 1 and client.beta.messages.calls == []


def test_claude_backend_refusal_and_errors():
    import anthropic

    client = _FakeClaudeClient(response=_Response(stop_reason="refusal"))
    with pytest.raises(CoachError, match="declined"):
        ClaudeBackend("claude-opus-5-5", client=client).complete(ChatRequest("s", "u", "claude-opus-5-5"))
    import httpx2 as httpx

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    auth_error = anthropic.AuthenticationError("bad key", response=httpx.Response(401, request=request), body=None)
    with pytest.raises(CoachError, match="API key missing or invalid"):
        ClaudeBackend("claude-opus-5-5", client=_FakeClaudeClient(fail=auth_error)).complete(ChatRequest("s", "u", "claude-opus-5-5"))
    rate = anthropic.RateLimitError("slow", response=httpx.Response(429, request=request), body=None)
    with pytest.raises(CoachError, match="rate limited"):
        ClaudeBackend("claude-opus-5-5", client=_FakeClaudeClient(fail=rate)).complete(ChatRequest("s", "u", "claude-opus-5-5"))
    with pytest.raises(CoachError, match="empty"):
        ClaudeBackend("claude-opus-5-5", client=_FakeClaudeClient(response=_Response(text="  "))).complete(ChatRequest("s", "u", "claude-opus-5-5"))


def test_openrouter_backend():
    opener = opener_with([
        {"data": {"label": "wristpull key"}},
        {"model": "anthropic/claude-sonnet-4.6", "choices": [{"message": {"role": "assistant", "content": "## Status\nfine"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 900, "completion_tokens": 120, "cost": 0.0042}},
    ])
    backend = OpenRouterBackend("anthropic/claude-sonnet-4.6", api_key="sk-or-test", opener=opener, sleep=lambda _s: None)
    assert backend.check() == "openrouter anthropic/claude-sonnet-4.6 ready (wristpull key)"
    response = backend.complete(ChatRequest("sys", "usr", "anthropic/claude-sonnet-4.6", max_tokens=400))
    assert response.text == "## Status\nfine" and response.cost_usd == 0.0042 and response.input_tokens == 900 and not response.truncated
    method, url, body, _ = opener.calls[1]
    assert url == "https://openrouter.ai/api/v1/chat/completions" and body["usage"] == {"include": True} and body["max_tokens"] == 400
    req_headers = None
    backend2 = OpenRouterBackend("m", api_key="sk", opener=opener_with([401]), sleep=lambda _s: None)
    with pytest.raises(CoachError, match="API key invalid"):
        backend2.complete(ChatRequest("s", "u", "m"))
    backend3 = OpenRouterBackend("m", api_key="sk", opener=opener_with([{"error": {"message": "model is down"}}]), sleep=lambda _s: None)
    with pytest.raises(CoachError, match="model is down"):
        backend3.complete(ChatRequest("s", "u", "m"))
    assert req_headers is None


def test_api_key_resolution_and_factory_kinds():
    config = Config()
    env = {"ANTHROPIC_API_KEY": "a", "OPENROUTER_API_KEY": "o", "MY_KEY": "m"}.get
    assert api_key_for(config, "claude", env) == "a" and api_key_for(config, "openrouter", env) == "o"
    config.coach.api_key_env = "MY_KEY"
    assert api_key_for(config, "claude", env) == "m"
    config.coach.api_key_env = None
    config.coach.weekly_model = "gemma3:12b"
    assert make_backend(config, kind="weekly").model == "gemma3:12b" and make_backend(config, kind="daily").model == "gemma3:4b"
    assert make_backend(config, kind="weekly", model="x").model == "x"
    claude = make_backend(config, backend="claude")
    assert isinstance(claude, ClaudeBackend) and claude.model == "gemma3:4b"  # model comes from the config, whatever it is
    import os

    saved = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        with pytest.raises(CoachError, match="needs an API key"):
            make_backend(config, backend="openrouter")
        os.environ["OPENROUTER_API_KEY"] = "sk-or"
        assert isinstance(make_backend(config, backend="openrouter"), OpenRouterBackend)
    finally:
        os.environ.pop("OPENROUTER_API_KEY", None)
        if saved is not None:
            os.environ["OPENROUTER_API_KEY"] = saved
