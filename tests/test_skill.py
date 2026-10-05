from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ovos_openclaw_skill import OpenClawSkill
from ovos_openclaw_skill.client import OpenClawError
from ovos_workshop.skills.fallback import FallbackSkill


def bare_skill():
    skill = object.__new__(OpenClawSkill)
    skill.speak = Mock()
    skill.log = Mock()
    skill._settings = {}
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


def test_direct_fallback_forwards_raw_utterance(monkeypatch):
    skill = bare_skill()
    client = Mock()
    client.complete.return_value = "Collins."
    skill._make_client = Mock(return_value=client)
    monkeypatch.setattr("ovos_openclaw_skill.get_gateway_token", lambda: "token")

    handled = skill.handle_direct_request(
        SimpleNamespace(data={"utterances": ["what is my name"]})
    )

    assert handled is True
    client.complete.assert_called_once_with("what is my name")
    skill.speak.assert_called_once_with("Collins.")


@pytest.mark.parametrize(
    "utterance",
    ["[BLANK_AUDIO]", "BLANK _ AUDIO", "BLANK_AUDIO", "BLANK AUDIO"],
)
def test_direct_fallback_consumes_blank_audio_without_speaking(
    monkeypatch, utterance
):
    skill = bare_skill()
    make_client = Mock()
    skill._make_client = make_client
    monkeypatch.setattr("ovos_openclaw_skill.get_gateway_token", lambda: "token")

    handled = skill.handle_direct_request(
        SimpleNamespace(data={"utterance": utterance})
    )

    assert handled is True
    make_client.assert_not_called()
    skill.speak.assert_not_called()


def test_direct_fallback_only_advertises_nonempty_utterances():
    skill = bare_skill()

    assert skill.can_answer(SimpleNamespace(data={"utterances": ["hello"]})) is True
    assert skill.can_answer(SimpleNamespace(data={"utterances": [""]})) is False
    assert skill.can_answer(SimpleNamespace(data={})) is False


def test_direct_fallback_can_be_disabled():
    skill = bare_skill()
    skill._settings["direct_route"] = False
    message = SimpleNamespace(data={"utterance": "what time is it"})

    assert skill.can_answer(message) is False
    assert skill.handle_direct_request(message) is False
    skill.speak.assert_not_called()


def test_skill_registers_intent_and_high_priority_fallback():
    assert issubclass(OpenClawSkill, FallbackSkill)
    assert "dravon.intent" in OpenClawSkill.handle_dravon.intents
    assert OpenClawSkill.handle_direct_request.fallback_priority == 1

