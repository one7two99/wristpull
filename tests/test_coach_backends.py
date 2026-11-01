import io
import json
import urllib.error

import pytest

from wristpull.coach._http import HttpError, get_json, post_json, with_retries
from wristpull.coach.backends import ChatRequest, CoachError, FakeBackend, OllamaBackend, estimate_tokens, make_backend
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
    with pytest.raises(CoachError, match="next coach wave"):
        make_backend(config, backend="claude")
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
