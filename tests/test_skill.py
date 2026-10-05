from types import SimpleNamespace
from unittest.mock import Mock

from ovos_openclaw_skill import OpenClawSkill
from ovos_openclaw_skill.client import OpenClawError


def bare_skill():
    skill = object.__new__(OpenClawSkill)
    skill.speak = Mock()
    skill.log = Mock()
    skill.shutdown = Mock()
    skill.default_shutdown = Mock()
    return skill


def test_handler_speaks_gateway_answer(monkeypatch):
    skill = bare_skill()
    client = Mock()
    client.complete.return_value = "It is sunny."
    skill._make_client = Mock(return_value=client)
    monkeypatch.setattr("ovos_openclaw_skill.get_gateway_token", lambda: "token")

    skill.handle_dravon(SimpleNamespace(data={"query": "weather today"}))

    client.complete.assert_called_once_with("weather today")
    skill.speak.assert_called_once_with("It is sunny.")


def test_handler_has_safe_error_speech(monkeypatch):
    skill = bare_skill()
    client = Mock()
    client.complete.side_effect = OpenClawError("Bearer highly-secret")
    skill._make_client = Mock(return_value=client)
    monkeypatch.setattr("ovos_openclaw_skill.get_gateway_token", lambda: "highly-secret")

    skill.handle_dravon(SimpleNamespace(data={"query": "hello"}))

    spoken = skill.speak.call_args.args[0]
    assert spoken == "I couldn't reach Dravon right now."
    assert "highly-secret" not in spoken


def test_handler_requires_token(monkeypatch):
    skill = bare_skill()
    monkeypatch.setattr("ovos_openclaw_skill.get_gateway_token", lambda: None)
    skill.handle_dravon(SimpleNamespace(data={"query": "hello"}))
    skill.speak.assert_called_once_with("Dravon is not configured yet.")


def test_padatious_intent_is_attached():
    assert "dravon.intent" in OpenClawSkill.handle_dravon.intents

