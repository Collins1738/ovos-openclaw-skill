from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from ovos_openclaw_skill import (
    END_LISTENING_SOUND,
    PLAY_SOUND_TOPIC,
    OpenClawSkill,
)
from ovos_openclaw_skill.client import OpenClawError
from ovos_workshop.skills.fallback import FallbackSkill


def bare_skill():
    skill = object.__new__(OpenClawSkill)
    skill.speak = Mock()
    skill.log = Mock()
    skill._settings = {"follow_up_enabled": False}
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


def test_follow_up_mode_routes_multiple_turns_without_wake_word(monkeypatch):
    skill = bare_skill()
    skill._settings.update(
        {"follow_up_enabled": True, "follow_up_max_turns": 3}
    )
    client = Mock()
    client.complete.side_effect = ["First answer.", "Second answer."]
    skill._make_client = Mock(return_value=client)
    skill.get_response = Mock(side_effect=["and what about tomorrow", None])
    monkeypatch.setattr("ovos_openclaw_skill.get_gateway_token", lambda: "token")
    message = SimpleNamespace(data={"utterance": "what about today"})

    handled = skill.handle_direct_request(message)

    assert handled is True
    assert client.complete.call_args_list == [
        call("what about today"),
        call("and what about tomorrow"),
    ]
    assert skill.speak.call_args_list == [
        call("First answer.", wait=60),
        call("Second answer.", wait=60),
    ]
    assert skill.get_response.call_args_list == [
        call(message=message, num_retries=0),
        call(message=message, num_retries=0),
    ]


def test_follow_up_mode_stops_silently_on_blank_audio(monkeypatch):
    skill = bare_skill()
    skill._settings.update({"follow_up_enabled": True})
    client = Mock()
    client.complete.return_value = "First answer."
    skill._make_client = Mock(return_value=client)
    skill.get_response = Mock(return_value="BLANK _ AUDIO")
    monkeypatch.setattr("ovos_openclaw_skill.get_gateway_token", lambda: "token")

    skill.handle_direct_request(SimpleNamespace(data={"utterance": "hello"}))

    client.complete.assert_called_once_with("hello")
    skill.speak.assert_called_once_with("First answer.", wait=60)


def test_follow_up_mode_enforces_turn_limit(monkeypatch):
    skill = bare_skill()
    skill._settings.update(
        {"follow_up_enabled": True, "follow_up_max_turns": 1}
    )
    client = Mock()
    client.complete.side_effect = ["First.", "Second."]
    skill._make_client = Mock(return_value=client)
    skill.get_response = Mock(return_value="follow up")
    monkeypatch.setattr("ovos_openclaw_skill.get_gateway_token", lambda: "token")

    skill.handle_direct_request(SimpleNamespace(data={"utterance": "initial"}))

    assert client.complete.call_count == 2
    skill.get_response.assert_called_once()
    assert skill.speak.call_args_list == [
        call("First.", wait=60),
        call("Second."),
    ]


def test_follow_up_mode_caps_requested_limit_at_twenty_five():
    skill = bare_skill()
    skill._settings.update(
        {"follow_up_enabled": True, "follow_up_max_turns": 99}
    )
    skill._answer = Mock(return_value=True)
    skill.get_response = Mock(return_value="next")
    message = SimpleNamespace(data={"utterance": "initial"})

    skill._conversation("initial", message)

    assert skill._answer.call_count == 26
    assert skill.get_response.call_count == 25


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


def test_proactive_speech_opens_follow_up_listening():
    skill = bare_skill()

    skill._speak_proactively("Reminder: hydrate.", "en-us")

    skill.speak.assert_called_once_with(
        "Reminder: hydrate.",
        expect_response=True,
        wait=45,
        meta={"proactive": True, "requested_lang": "en-us"},
    )


def test_record_end_plays_the_packaged_completion_cue():
    skill = bare_skill()
    skill._bus = Mock()
    message = Mock()
    forwarded = Mock()
    message.forward.return_value = forwarded

    skill._handle_record_end(message)

    message.forward.assert_called_once_with(
        PLAY_SOUND_TOPIC,
        {"uri": END_LISTENING_SOUND},
    )
    skill.bus.emit.assert_called_once_with(forwarded)


def test_proactive_response_and_result_never_echo_spoken_text():
    skill = bare_skill()
    skill.skill_id = "ovos-openclaw-skill"
    skill._bus = Mock()
    skill._proactive_speech = Mock()
    skill._proactive_speech.submit.return_value = {
        "v": 1,
        "request_id": "abc",
        "status": "accepted",
        "reason": None,
        "queue_depth": 1,
    }
    message = Mock()
    message.data = {"text": "private reminder"}
    message.response.return_value = "response-message"

    skill._handle_proactive_speech(message)
    skill._emit_proactive_result({
        "v": 1,
        "request_id": "abc",
        "status": "spoken",
        "finished_at": 1,
    })

    assert skill.bus.emit.call_args_list[0].args[0] == "response-message"
    result_message = skill.bus.emit.call_args_list[1].args[0]
    assert "text" not in result_message.data
    assert result_message.msg_type.endswith("proactive_speech.result")


def test_skill_registers_intent_and_high_priority_fallback():
    assert issubclass(OpenClawSkill, FallbackSkill)
    assert "dravon.intent" in OpenClawSkill.handle_dravon.intents
    assert OpenClawSkill.handle_direct_request.fallback_priority == 1

