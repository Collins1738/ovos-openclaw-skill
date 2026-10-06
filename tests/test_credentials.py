from subprocess import CompletedProcess

from ovos_openclaw_skill import credentials


def test_environment_token_has_priority(monkeypatch):
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "from-env")
    monkeypatch.setattr(credentials.subprocess, "run", lambda *a, **k: None)
    assert credentials.get_gateway_token() == "from-env"


def test_speech_ingress_uses_independent_credentials(monkeypatch):
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "gateway-secret")
    monkeypatch.setenv("OVOS_OPENCLAW_SPEECH_SECRET", "speech-secret")
    monkeypatch.setenv("OVOS_OPENCLAW_HTTP_TOKEN", "http-token")

    assert credentials.get_gateway_token() == "gateway-secret"
    assert credentials.get_proactive_speech_secret() == "speech-secret"
    assert credentials.get_speech_http_token() == "http-token"


def test_macos_keychain_fallback(monkeypatch):
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    monkeypatch.setattr(credentials.sys, "platform", "darwin")
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return CompletedProcess(command, 0, "from-keychain\n", "")

    monkeypatch.setattr(credentials.subprocess, "run", fake_run)
    assert credentials.get_gateway_token() == "from-keychain"
    assert seen["command"][-5:] == [
        "-s", "ovos-openclaw-skill", "-a", "gateway-token", "-w"
    ]

