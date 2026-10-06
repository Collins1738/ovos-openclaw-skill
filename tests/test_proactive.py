import json
import sys
import uuid
from threading import Event
from unittest.mock import Mock

import pytest

from ovos_bus_client import Message
from ovos_openclaw_skill.proactive import (
    MAX_CHARACTERS,
    ProactiveSpeechError,
    ProactiveSpeechManager,
    RESPONSE_TOPIC,
    RESULT_TOPIC,
    SpeechRequest,
    make_request,
    sanitize_text,
    send_request,
    sign_request,
    verify_signature,
)


SECRET = "a" * 43
NOW = 1_791_302_400


def request(text="Your package was delivered.", request_id=None):
    return make_request(
        text,
        "en-us",
        SECRET,
        request_id=request_id,
        now=NOW,
    )


def manager(*, speak=None, result=None):
    instance = ProactiveSpeechManager(
        speak=speak or Mock(),
        result=result or Mock(),
        secret_provider=lambda: SECRET,
        now=lambda: NOW,
    )
    instance.start()
    return instance


def test_signature_round_trip_and_tamper_detection():
    data = request()
    item = SpeechRequest(
        data["request_id"],
        data["issued_at"],
        data["expires_at"],
        data["text"],
        data["lang"],
    )
    assert verify_signature(item, data["signature"], SECRET) is True
    tampered = SpeechRequest(
        item.request_id, item.issued_at, item.expires_at, "Tampered.", item.lang
    )
    assert verify_signature(tampered, data["signature"], SECRET) is False
    assert sign_request(item, SECRET).startswith("hmac-sha256:")


def test_valid_request_is_spoken_once_without_echoing_text_in_result():
    spoken = Mock()
    results = []
    completed = Event()

    def result(payload):
        results.append(payload)
        completed.set()

    instance = manager(speak=spoken, result=result)
    try:
        response = instance.submit(request())
        assert response["status"] == "accepted"
        assert completed.wait(1)
        spoken.assert_called_once_with("Your package was delivered.", "en-us")
        assert results[0]["status"] == "spoken"
        assert "text" not in results[0]
    finally:
        instance.stop()


def test_replayed_request_is_a_duplicate_and_not_spoken_twice():
    spoken = Mock()
    completed = Event()
    instance = manager(speak=spoken, result=lambda _: completed.set())
    data = request(request_id=str(uuid.uuid4()))
    try:
        assert instance.submit(data)["status"] == "accepted"
        assert completed.wait(1)
        duplicate = instance.submit(data)
        assert duplicate["status"] == "duplicate"
        assert duplicate["reason"] == "duplicate"
        spoken.assert_called_once()
    finally:
        instance.stop()


def test_tampered_request_is_rejected_without_secret_or_text_in_response():
    instance = manager()
    data = request()
    data["text"] = "Injected speech."
    try:
        response = instance.submit(data)
        assert response["status"] == "rejected"
        assert response["reason"] == "unauthorized"
        assert SECRET not in json.dumps(response)
        assert "Injected" not in json.dumps(response)
    finally:
        instance.stop()


def test_expired_and_future_requests_are_rejected():
    instance = manager()
    expired = request()
    expired["issued_at"] = NOW - 100
    expired["expires_at"] = NOW - 1
    item = SpeechRequest(
        expired["request_id"], expired["issued_at"], expired["expires_at"],
        expired["text"], expired["lang"]
    )
    expired["signature"] = sign_request(item, SECRET)
    future = request()
    future["issued_at"] = NOW + 31
    future["expires_at"] = NOW + 60
    item = SpeechRequest(
        future["request_id"], future["issued_at"], future["expires_at"],
        future["text"], future["lang"]
    )
    future["signature"] = sign_request(item, SECRET)
    try:
        assert instance.submit(expired)["reason"] == "expired"
        assert instance.submit(future)["reason"] == "expired"
    finally:
        instance.stop()


def test_text_bounds_and_controls():
    assert sanitize_text("  hello\n there  ") == "hello there"
    with pytest.raises(ProactiveSpeechError, match="malformed"):
        sanitize_text("hello\x00there")
    with pytest.raises(ProactiveSpeechError, match="too_large"):
        sanitize_text("x" * (MAX_CHARACTERS + 1))
    with pytest.raises(ProactiveSpeechError, match="too_large"):
        sanitize_text("word " * 71)


class FakeBus:
    def __init__(self):
        self.connected_event = Event()
        self.connected_event.set()
        self.handlers = {}
        self.emitted = []
        self.closed = False

    def on(self, name, handler):
        self.handlers[name] = handler

    def remove(self, name, handler):
        self.handlers.pop(name, None)

    def run_in_thread(self):
        return None

    def emit(self, message):
        self.emitted.append(message)
        request_id = message.data["request_id"]
        self.handlers[RESPONSE_TOPIC](Message(RESPONSE_TOPIC, {
            "v": 1, "request_id": request_id, "status": "accepted",
            "reason": None, "queue_depth": 1,
        }))
        self.handlers[RESULT_TOPIC](Message(RESULT_TOPIC, {
            "v": 1, "request_id": request_id, "status": "spoken",
            "finished_at": NOW,
        }))

    def close(self):
        self.closed = True


def test_sender_waits_for_correlated_spoken_result():
    bus = FakeBus()
    data = request()
    result = send_request(data, 1, client_factory=lambda: bus)
    assert result["status"] == "spoken"
    assert bus.emitted[0].msg_type.endswith("proactive_speech.request")
    assert bus.closed is True


def test_make_request_rejects_missing_secret_and_invalid_id():
    with pytest.raises(ProactiveSpeechError, match="not configured"):
        make_request("hello", "en-us", "")
    with pytest.raises(ProactiveSpeechError, match="request id"):
        make_request("hello", "en-us", SECRET, request_id="not-a-uuid")
