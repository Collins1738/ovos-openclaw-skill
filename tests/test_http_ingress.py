import json
import uuid
from threading import Event, Thread
from unittest.mock import Mock

from ovos_openclaw_skill.http_ingress import (
    HTTPIdempotencyStore,
    SpeechHTTPApplication,
)


HTTP_TOKEN = "h" * 43
SPEECH_SECRET = "s" * 43


def headers(request_id=None, token=HTTP_TOKEN):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Idempotency-Key": request_id or str(uuid.uuid4()),
    }


def app(submit=None, idempotency=None):
    return SpeechHTTPApplication(
        token_provider=lambda: HTTP_TOKEN,
        speech_secret_provider=lambda: SPEECH_SECRET,
        submit=submit or Mock(return_value={
            "status": "accepted", "reason": None, "queue_depth": 1,
        }),
        idempotency=idempotency,
        default_lang="en-us",
    )


def test_health_is_minimal_and_does_not_require_authentication():
    status, response = app().handle("GET", "/health", {}, b"")
    assert status == 200
    assert response == {"v": 1, "status": "ok"}


def test_speak_requires_a_valid_bearer_token():
    submit = Mock()
    status, response = app(submit).handle(
        "POST", "/v1/speak", headers(token="wrong"),
        json.dumps({"text": "hello"}).encode(),
    )
    assert status == 401
    assert response["error"] == "unauthorized"
    submit.assert_not_called()


def test_speak_submits_a_signed_request_without_echoing_text():
    submit = Mock(return_value={
        "status": "accepted", "reason": None, "queue_depth": 1,
    })
    request_id = str(uuid.uuid4())
    status, response = app(submit).handle(
        "POST", "/v1/speak", headers(request_id),
        json.dumps({"text": "Your laundry is ready.", "lang": "en-us"}).encode(),
    )
    assert status == 202
    assert response == {
        "v": 1, "request_id": request_id,
        "status": "accepted", "queue_depth": 1,
    }
    request = submit.call_args.args[0]
    assert request["request_id"] == request_id
    assert request["text"] == "Your laundry is ready."
    assert request["signature"].startswith("hmac-sha256:")
    assert "text" not in response


def test_funnel_prefixed_path_is_supported():
    status, response = app().handle(
        "POST", "/speech/v1/speak", headers(),
        json.dumps({"text": "hello"}).encode(),
    )
    assert status == 202
    assert response["status"] == "accepted"


def test_duplicate_is_an_idempotent_success():
    submit = Mock(return_value={
        "status": "accepted", "reason": None, "queue_depth": 1,
    })
    application = app(submit)
    request_id = str(uuid.uuid4())
    request_headers = headers(request_id)
    body = json.dumps({"text": "hello"}).encode()

    assert application.handle("POST", "/v1/speak", request_headers, body)[0] == 202
    status, response = application.handle(
        "POST", "/v1/speak", request_headers, body
    )
    assert status == 200
    assert response["status"] == "duplicate"
    submit.assert_called_once()


def test_reusing_an_idempotency_key_for_other_text_is_a_conflict():
    application = app()
    request_headers = headers(str(uuid.uuid4()))
    first = json.dumps({"text": "first"}).encode()
    second = json.dumps({"text": "second"}).encode()

    assert application.handle("POST", "/v1/speak", request_headers, first)[0] == 202
    status, response = application.handle(
        "POST", "/v1/speak", request_headers, second
    )
    assert status == 409
    assert response["error"] == "idempotency_conflict"


def test_concurrent_duplicate_waits_for_the_first_admission():
    started = Event()
    release = Event()

    def submit(_request):
        started.set()
        assert release.wait(2)
        return {"status": "accepted", "reason": None, "queue_depth": 1}

    application = app(submit)
    request_headers = headers(str(uuid.uuid4()))
    body = json.dumps({"text": "hello"}).encode()
    results = []
    first = Thread(target=lambda: results.append(
        application.handle("POST", "/v1/speak", request_headers, body)
    ))
    second = Thread(target=lambda: results.append(
        application.handle("POST", "/v1/speak", request_headers, body)
    ))
    first.start()
    assert started.wait(1)
    second.start()
    release.set()
    first.join(2)
    second.join(2)

    assert sorted(status for status, _ in results) == [200, 202]
    assert sorted(response["status"] for _, response in results) == ["accepted", "duplicate"]


def test_idempotency_survives_a_service_restart(tmp_path):
    path = tmp_path / "idempotency.json"
    request_id = str(uuid.uuid4())
    request_headers = headers(request_id)
    body = json.dumps({"text": "hello"}).encode()
    first_submit = Mock(return_value={
        "status": "accepted", "reason": None, "queue_depth": 1,
    })
    first = app(first_submit, HTTPIdempotencyStore(path))
    assert first.handle("POST", "/v1/speak", request_headers, body)[0] == 202

    second_submit = Mock()
    second = app(second_submit, HTTPIdempotencyStore(path))
    status, response = second.handle(
        "POST", "/v1/speak", request_headers, body
    )
    assert status == 200
    assert response["status"] == "duplicate"
    second_submit.assert_not_called()
    assert path.stat().st_mode & 0o777 == 0o600


def test_rejected_admission_releases_the_idempotency_key_for_retry():
    submit = Mock(side_effect=[
        {"status": "rejected", "reason": "rate_limited", "queue_depth": 0},
        {"status": "accepted", "reason": None, "queue_depth": 1},
    ])
    application = app(submit)
    request_headers = headers(str(uuid.uuid4()))
    body = json.dumps({"text": "hello"}).encode()

    assert application.handle("POST", "/v1/speak", request_headers, body)[0] == 429
    assert application.handle("POST", "/v1/speak", request_headers, body)[0] == 202
    assert submit.call_count == 2


def test_speak_rejects_unknown_fields_and_invalid_idempotency_keys():
    status, response = app().handle(
        "POST", "/v1/speak", headers(),
        json.dumps({"text": "hello", "command": "whoami"}).encode(),
    )
    assert status == 400
    assert response["error"] == "malformed"

    status, response = app().handle(
        "POST", "/v1/speak", headers(request_id="not-a-uuid"),
        json.dumps({"text": "hello"}).encode(),
    )
    assert status == 400
    assert response["error"] == "malformed"


def test_speak_rejects_non_json_content():
    request_headers = headers()
    request_headers["Content-Type"] = "text/plain"
    status, response = app().handle(
        "POST", "/v1/speak", request_headers, b"hello"
    )
    assert status == 415
    assert response["error"] == "content_type_not_supported"
