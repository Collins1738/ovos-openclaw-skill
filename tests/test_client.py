import json
from urllib.error import URLError

import pytest

from ovos_openclaw_skill.client import OpenClawClient, OpenClawError


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def test_completion_posts_openai_payload(monkeypatch):
    observed = {}

    def fake_urlopen(request, timeout):
        observed["request"] = request
        observed["timeout"] = timeout
        return FakeResponse({"choices": [{"message": {"content": "A short answer."}}]})

    monkeypatch.setattr("ovos_openclaw_skill.client.urlopen", fake_urlopen)
    client = OpenClawClient(
        "http://127.0.0.1:18789", "secret-value", conversation="living-room", max_tokens=90
    )

    assert client.complete("what time is it") == "A short answer."
    request = observed["request"]
    body = json.loads(request.data)
    assert request.full_url == "http://127.0.0.1:18789/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer secret-value"
    assert body["model"] == "openclaw/default"
    assert body["user"] == "living-room"
    assert body["max_tokens"] == 90
    assert body["messages"][-1] == {"role": "user", "content": "what time is it"}
    assert observed["timeout"] == 60.0


def test_transport_errors_are_wrapped_without_secret(monkeypatch):
    monkeypatch.setattr(
        "ovos_openclaw_skill.client.urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(URLError("secret-value")),
    )
    with pytest.raises(OpenClawError) as error:
        OpenClawClient("http://127.0.0.1:18789", "secret-value").complete("hello")
    assert "secret-value" not in str(error.value)


def test_non_loopback_gateway_is_rejected_before_request(monkeypatch):
    called = False

    def fake_urlopen(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("ovos_openclaw_skill.client.urlopen", fake_urlopen)
    with pytest.raises(OpenClawError, match="loopback"):
        OpenClawClient("https://example.com", "secret-value").complete("hello")
    assert called is False


def test_token_echo_is_rejected(monkeypatch):
    monkeypatch.setattr(
        "ovos_openclaw_skill.client.urlopen",
        lambda *args, **kwargs: FakeResponse(
            {"choices": [{"message": {"content": "token is secret-value"}}]}
        ),
    )
    with pytest.raises(OpenClawError, match="unsafe"):
        OpenClawClient("http://127.0.0.1:18789", "secret-value").complete("hello")

