"""Authenticated local ingress for proactive OVOS speech."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import queue
import re
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass
from threading import Event, Lock, Thread
from typing import Any, Callable, Sequence

from ovos_bus_client import Message, MessageBusClient
from ovos_config import Configuration

from .credentials import get_proactive_speech_secret


REQUEST_TOPIC = "ovos.openclaw.proactive_speech.request"
RESPONSE_TOPIC = f"{REQUEST_TOPIC}.response"
RESULT_TOPIC = "ovos.openclaw.proactive_speech.result"
PROTOCOL_VERSION = 1
MAX_CHARACTERS = 750
MAX_UTF8_BYTES = 4096
MAX_WORDS = 100
MAX_LIFETIME_SECONDS = 120
MAX_FUTURE_SKEW_SECONDS = 30
QUEUE_CAPACITY = 5
MAX_PER_MINUTE = 3
MAX_PER_HOUR = 12
DEDUPE_CAPACITY = 2048
DEDUPE_TTL_SECONDS = 24 * 60 * 60
DEFAULT_REQUEST_LIFETIME = 60
DEFAULT_WAIT_TIMEOUT = 70.0
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_REQUEST_FIELDS = {
    "v", "request_id", "issued_at", "expires_at", "text", "lang", "signature"
}


class ProactiveSpeechError(RuntimeError):
    """A safe failure that never contains message text or credentials."""


@dataclass(frozen=True)
class SpeechRequest:
    request_id: str
    issued_at: int
    expires_at: int
    text: str
    lang: str


def sanitize_text(text: str) -> str:
    """Normalize authenticated text after enforcing bounded plain text."""
    if _CONTROL_CHARACTERS.search(text):
        raise ProactiveSpeechError("malformed")
    if len(text) > MAX_CHARACTERS or len(text.encode("utf-8")) > MAX_UTF8_BYTES:
        raise ProactiveSpeechError("too_large")
    normalized = " ".join(text.split()).strip()
    if not normalized:
        raise ProactiveSpeechError("malformed")
    if len(normalized.split()) > MAX_WORDS:
        raise ProactiveSpeechError("too_large")
    return normalized


def signature_payload(request: SpeechRequest) -> bytes:
    text_hash = hashlib.sha256(request.text.encode("utf-8")).hexdigest()
    return "\n".join([
        REQUEST_TOPIC,
        str(PROTOCOL_VERSION),
        request.request_id,
        str(request.issued_at),
        str(request.expires_at),
        request.lang,
        text_hash,
    ]).encode("utf-8")


def sign_request(request: SpeechRequest, secret: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), signature_payload(request), hashlib.sha256
    ).digest()
    encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"hmac-sha256:{encoded}"


def verify_signature(request: SpeechRequest, signature: str, secret: str) -> bool:
    return hmac.compare_digest(sign_request(request, secret), signature)


def make_request(
    text: str,
    lang: str,
    secret: str,
    *,
    request_id: str | None = None,
    now: int | None = None,
    lifetime: int = DEFAULT_REQUEST_LIFETIME,
) -> dict[str, Any]:
    if not secret or len(secret) < 32:
        raise ProactiveSpeechError("speech ingress is not configured")
    if lifetime <= 0 or lifetime > MAX_LIFETIME_SECONDS:
        raise ProactiveSpeechError("invalid request lifetime")
    sanitize_text(text)
    issued_at = int(time.time() if now is None else now)
    identifier = request_id or str(uuid.uuid4())
    try:
        parsed = uuid.UUID(identifier)
    except ValueError as error:
        raise ProactiveSpeechError("invalid request id") from error
    if parsed.version != 4 or str(parsed) != identifier:
        raise ProactiveSpeechError("invalid request id")
    request = SpeechRequest(identifier, issued_at, issued_at + lifetime, text, lang)
    return {
        "v": PROTOCOL_VERSION,
        "request_id": request.request_id,
        "issued_at": request.issued_at,
        "expires_at": request.expires_at,
        "text": request.text,
        "lang": request.lang,
        "signature": sign_request(request, secret),
    }


class ProactiveSpeechManager:
    """Authenticate, bound, queue, and serialize proactive announcements."""

    def __init__(
        self,
        *,
        speak: Callable[[str, str], None],
        result: Callable[[dict[str, Any]], None],
        secret_provider: Callable[[], str | None] = get_proactive_speech_secret,
        now: Callable[[], float] = time.time,
    ):
        self._speak = speak
        self._result = result
        self._secret_provider = secret_provider
        self._now = now
        self._queue: queue.Queue[SpeechRequest | None] = queue.Queue(maxsize=QUEUE_CAPACITY)
        self._lock = Lock()
        self._seen: dict[str, float] = {}
        self._minute: deque[float] = deque()
        self._hour: deque[float] = deque()
        self._accepting = False
        self._worker: Thread | None = None

    def start(self) -> None:
        with self._lock:
            if self._accepting:
                return
            self._accepting = True
        self._worker = Thread(
            target=self._run, name="ovos-openclaw-proactive-speech", daemon=True
        )
        self._worker.start()

    def stop(self, timeout: float = 3.0) -> None:
        with self._lock:
            self._accepting = False
        while True:
            try:
                pending = self._queue.get_nowait()
            except queue.Empty:
                break
            if pending is not None:
                self._emit_result(pending.request_id, "shutdown")
            self._queue.task_done()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._worker:
            self._worker.join(timeout)

    def submit(self, data: Any) -> dict[str, Any]:
        request_id = data.get("request_id") if isinstance(data, dict) else None
        response = {
            "v": PROTOCOL_VERSION,
            "request_id": request_id if isinstance(request_id, str) else None,
            "status": "rejected",
            "reason": "malformed",
            "queue_depth": self._queue.qsize(),
        }
        try:
            request, signature = self._parse(data)
            response["request_id"] = request.request_id
            secret = self._secret_provider()
            if not secret or len(secret) < 32:
                raise ProactiveSpeechError("unavailable")
            if not verify_signature(request, signature, secret):
                raise ProactiveSpeechError("unauthorized")
            request = SpeechRequest(
                request.request_id,
                request.issued_at,
                request.expires_at,
                sanitize_text(request.text),
                request.lang,
            )
            self._admit(request)
        except ProactiveSpeechError as error:
            response["reason"] = str(error)
            if response["reason"] == "duplicate":
                response["status"] = "duplicate"
            return response
        except Exception:
            response["reason"] = "malformed"
            return response
        response.update({
            "status": "accepted", "reason": None, "queue_depth": self._queue.qsize()
        })
        return response

    def _parse(self, data: Any) -> tuple[SpeechRequest, str]:
        if not isinstance(data, dict) or set(data) != _REQUEST_FIELDS:
            raise ProactiveSpeechError("malformed")
        if data.get("v") != PROTOCOL_VERSION:
            raise ProactiveSpeechError("malformed")
        for key in ("request_id", "text", "lang", "signature"):
            if not isinstance(data.get(key), str):
                raise ProactiveSpeechError("malformed")
        for key in ("issued_at", "expires_at"):
            if not isinstance(data.get(key), int) or isinstance(data.get(key), bool):
                raise ProactiveSpeechError("malformed")
        if len(data["text"]) > MAX_CHARACTERS or len(data["text"].encode("utf-8")) > MAX_UTF8_BYTES:
            raise ProactiveSpeechError("too_large")
        if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?", data["lang"]):
            raise ProactiveSpeechError("malformed")
        try:
            parsed_id = uuid.UUID(data["request_id"])
        except ValueError as error:
            raise ProactiveSpeechError("malformed") from error
        if parsed_id.version != 4 or str(parsed_id) != data["request_id"]:
            raise ProactiveSpeechError("malformed")
        return SpeechRequest(
            data["request_id"], data["issued_at"], data["expires_at"],
            data["text"], data["lang"]
        ), data["signature"]

    def _admit(self, request: SpeechRequest) -> None:
        now = self._now()
        if request.issued_at > now + MAX_FUTURE_SKEW_SECONDS:
            raise ProactiveSpeechError("expired")
        if request.expires_at <= now:
            raise ProactiveSpeechError("expired")
        lifetime = request.expires_at - request.issued_at
        if lifetime <= 0 or lifetime > MAX_LIFETIME_SECONDS:
            raise ProactiveSpeechError("expired")
        with self._lock:
            if not self._accepting:
                raise ProactiveSpeechError("unavailable")
            self._prune(now)
            if request.request_id in self._seen:
                raise ProactiveSpeechError("duplicate")
            if len(self._minute) >= MAX_PER_MINUTE or len(self._hour) >= MAX_PER_HOUR:
                raise ProactiveSpeechError("rate_limited")
            self._seen[request.request_id] = now
            self._minute.append(now)
            self._hour.append(now)
            try:
                self._queue.put_nowait(request)
            except queue.Full as error:
                self._seen.pop(request.request_id, None)
                self._minute.pop()
                self._hour.pop()
                raise ProactiveSpeechError("queue_full") from error

    def _prune(self, now: float) -> None:
        while self._minute and self._minute[0] <= now - 60:
            self._minute.popleft()
        while self._hour and self._hour[0] <= now - 3600:
            self._hour.popleft()
        self._seen = {
            key: seen_at for key, seen_at in self._seen.items()
            if seen_at > now - DEDUPE_TTL_SECONDS
        }
        if len(self._seen) > DEDUPE_CAPACITY:
            newest = sorted(self._seen.items(), key=lambda item: item[1], reverse=True)
            self._seen = dict(newest[:DEDUPE_CAPACITY])

    def _run(self) -> None:
        while True:
            request = self._queue.get()
            try:
                if request is None:
                    return
                if request.expires_at <= self._now():
                    self._emit_result(request.request_id, "expired")
                else:
                    try:
                        self._speak(request.text, request.lang)
                    except Exception:
                        self._emit_result(request.request_id, "failed")
                    else:
                        self._emit_result(request.request_id, "spoken")
            finally:
                self._queue.task_done()

    def _emit_result(self, request_id: str, status: str) -> None:
        self._result({
            "v": PROTOCOL_VERSION,
            "request_id": request_id,
            "status": status,
            "finished_at": int(self._now()),
        })


def submit_request(
    data: dict[str, Any], timeout: float = 10.0,
    client_factory: Any = MessageBusClient,
) -> dict[str, Any]:
    """Send one authenticated request and wait only for queue admission."""
    client = client_factory()
    accepted = Event()
    response: dict[str, Any] = {}
    request_id = data["request_id"]

    def on_response(message: Message) -> None:
        if message.data.get("request_id") == request_id:
            response.update(message.data)
            accepted.set()

    try:
        client.on(RESPONSE_TOPIC, on_response)
        client.run_in_thread()
        if not client.connected_event.wait(timeout):
            raise ProactiveSpeechError("could not reach the local OVOS message bus")
        client.emit(Message(REQUEST_TOPIC, data))
        if not accepted.wait(timeout):
            raise ProactiveSpeechError("OVOS did not acknowledge the request")
        return response
    finally:
        try:
            client.remove(RESPONSE_TOPIC, on_response)
        except Exception:
            pass
        client.close()


def send_request(
    data: dict[str, Any], timeout: float, client_factory: Any = MessageBusClient
) -> dict[str, Any]:
    """Send one authenticated request and wait for admission and completion."""
    client = client_factory()
    accepted = Event()
    finished = Event()
    response: dict[str, Any] = {}
    result: dict[str, Any] = {}
    request_id = data["request_id"]

    def on_response(message: Message) -> None:
        if message.data.get("request_id") == request_id:
            response.update(message.data)
            accepted.set()

    def on_result(message: Message) -> None:
        if message.data.get("request_id") == request_id:
            result.update(message.data)
            finished.set()

    try:
        client.on(RESPONSE_TOPIC, on_response)
        client.on(RESULT_TOPIC, on_result)
        client.run_in_thread()
        if not client.connected_event.wait(10):
            raise ProactiveSpeechError("could not reach the local OVOS message bus")
        client.emit(Message(REQUEST_TOPIC, data))
        if not accepted.wait(10):
            raise ProactiveSpeechError("OVOS did not acknowledge the request")
        if response.get("status") == "duplicate":
            return response
        if response.get("status") != "accepted":
            raise ProactiveSpeechError(str(response.get("reason") or "rejected"))
        if not finished.wait(timeout):
            raise ProactiveSpeechError("speech playback did not complete in time")
        if result.get("status") != "spoken":
            raise ProactiveSpeechError(str(result.get("status") or "failed"))
        return result
    finally:
        for topic, handler in ((RESPONSE_TOPIC, on_response), (RESULT_TOPIC, on_result)):
            try:
                client.remove(topic, handler)
            except Exception:
                pass
        client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ovos-openclaw-speak",
        description="Send an authenticated proactive announcement to OVOS/Piper.",
    )
    parser.add_argument("text", nargs="?", help="Plain text to speak")
    parser.add_argument("--stdin", action="store_true", help="Read text from stdin")
    parser.add_argument("--lang", default=None, help="OVOS language tag")
    parser.add_argument("--request-id", help="Lowercase UUIDv4 idempotency key")
    parser.add_argument("--timeout", type=float, default=DEFAULT_WAIT_TIMEOUT)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Validate and sign without sending; never prints the signature",
    )
    return parser


def run_cli(argv: Sequence[str] | None = None) -> dict[str, Any]:
    args = build_parser().parse_args(argv)
    if bool(args.text) == bool(args.stdin):
        raise ProactiveSpeechError("provide text or --stdin, but not both")
    if args.timeout <= 0 or args.timeout > 300:
        raise ProactiveSpeechError("timeout must be between 0 and 300 seconds")
    text = sys.stdin.read() if args.stdin else args.text
    lang = args.lang or str(Configuration().get("lang", "en-us"))
    data = make_request(
        text, lang, get_proactive_speech_secret() or "", request_id=args.request_id
    )
    if args.dry_run:
        return {
            "status": "ready",
            "request_id": data["request_id"],
            "characters": len(sanitize_text(text)),
        }
    return send_request(data, args.timeout)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = run_cli(argv)
    except (ProactiveSpeechError, ValueError) as error:
        print(json.dumps({"status": "error", "error": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
