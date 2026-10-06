"""Narrow HTTP ingress for authenticated proactive speech requests."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore, Event, Lock, Timer
from typing import Any, Callable, Mapping, Sequence

from ovos_config import Configuration

from .credentials import get_proactive_speech_secret, get_speech_http_token
from .proactive import ProactiveSpeechError, make_request, submit_request


LOGGER = logging.getLogger(__name__)
PROTOCOL_VERSION = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8190
MAX_HTTP_BODY_BYTES = 8192
REQUEST_TIMEOUT_SECONDS = 10
REQUEST_DEADLINE_SECONDS = 30
MAX_CONCURRENT_REQUESTS = 16
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
SPEAK_PATHS = {"/v1/speak", "/speech/v1/speak"}
HEALTH_PATHS = {"/health", "/speech/health"}


class HTTPIdempotencyStore:
    """Persist payload hashes so public retries remain at-most-once."""

    def __init__(self, path: Path | None = None, now: Callable[[], float] = time.time):
        self._path = path
        self._now = now
        self._lock = Lock()
        self._pending: dict[str, Event] = {}
        self._records: dict[str, dict[str, Any]] = {}
        if path and path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("invalid HTTP idempotency store")
            for request_id, record in loaded.items():
                if (
                    isinstance(request_id, str)
                    and isinstance(record, dict)
                    and isinstance(record.get("payload_hash"), str)
                    and isinstance(record.get("accepted_at"), (int, float))
                ):
                    self._records[request_id] = record
                else:
                    raise ValueError("invalid HTTP idempotency store")
            self._prune(self._now())

    def begin(self, request_id: str, payload_hash: str) -> tuple[str, Event | None]:
        with self._lock:
            self._prune(self._now())
            record = self._records.get(request_id)
            if record:
                if record["payload_hash"] != payload_hash:
                    return "conflict", None
                pending = self._pending.get(request_id)
                return ("pending", pending) if pending else ("duplicate", None)
            event = Event()
            self._records[request_id] = {
                "payload_hash": payload_hash,
                "accepted_at": self._now(),
            }
            self._pending[request_id] = event
            try:
                self._save()
            except OSError:
                self._pending.pop(request_id, None)
                self._records.pop(request_id, None)
                raise
            return "new", event

    def finish(self, request_id: str, *, keep: bool) -> None:
        with self._lock:
            event = self._pending.pop(request_id, None)
            if not keep:
                self._records.pop(request_id, None)
            try:
                self._save()
            finally:
                if event:
                    event.set()

    def contains(self, request_id: str) -> bool:
        with self._lock:
            return request_id in self._records

    def _prune(self, now: float) -> None:
        cutoff = now - IDEMPOTENCY_TTL_SECONDS
        self._records = {
            request_id: record
            for request_id, record in self._records.items()
            if record["accepted_at"] > cutoff
        }

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self._records, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, self._path)


class SpeechHTTPApplication:
    """Validate HTTP requests and submit bounded messages to the OVOS bus."""

    def __init__(
        self,
        *,
        token_provider: Callable[[], str | None] = get_speech_http_token,
        speech_secret_provider: Callable[[], str | None] = get_proactive_speech_secret,
        submit: Callable[[dict[str, Any]], dict[str, Any]] = submit_request,
        idempotency: HTTPIdempotencyStore | None = None,
        default_lang: str = "en-us",
    ):
        self._token_provider = token_provider
        self._speech_secret_provider = speech_secret_provider
        self._submit = submit
        self._idempotency = idempotency or HTTPIdempotencyStore()
        self._default_lang = default_lang

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str:
        target = name.lower()
        for key, value in headers.items():
            if key.lower() == target:
                return value.strip()
        return ""

    def is_authorized(self, headers: Mapping[str, str]) -> bool:
        expected = self._token_provider() or ""
        authorization = self._header(headers, "Authorization")
        supplied = (
            authorization.removeprefix("Bearer ")
            if authorization.startswith("Bearer ") else ""
        )
        return len(expected) >= 32 and hmac.compare_digest(supplied, expected)

    def handle(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[int, dict[str, Any]]:
        if method == "GET" and path in HEALTH_PATHS:
            return 200, {"v": PROTOCOL_VERSION, "status": "ok"}
        if method != "POST" or path not in SPEAK_PATHS:
            return 404, {"v": PROTOCOL_VERSION, "status": "error", "error": "not_found"}

        if not self.is_authorized(headers):
            return 401, {"v": PROTOCOL_VERSION, "status": "error", "error": "unauthorized"}

        if len(body) > MAX_HTTP_BODY_BYTES:
            return 413, {"v": PROTOCOL_VERSION, "status": "error", "error": "too_large"}
        if self._header(headers, "Content-Type").split(";", 1)[0].lower() != "application/json":
            return 415, {
                "v": PROTOCOL_VERSION, "status": "error",
                "error": "content_type_not_supported",
            }
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, {"v": PROTOCOL_VERSION, "status": "error", "error": "malformed"}
        if not isinstance(payload, dict) or not set(payload).issubset({"text", "lang"}):
            return 400, {"v": PROTOCOL_VERSION, "status": "error", "error": "malformed"}
        text = payload.get("text")
        lang = payload.get("lang", self._default_lang)
        request_id = self._header(headers, "Idempotency-Key")
        if not isinstance(text, str) or not isinstance(lang, str) or not request_id:
            return 400, {"v": PROTOCOL_VERSION, "status": "error", "error": "malformed"}

        reserved = False
        try:
            request = make_request(
                text,
                lang,
                self._speech_secret_provider() or "",
                request_id=request_id,
            )
            payload_hash = hashlib.sha256(
                json.dumps(
                    {"text": text, "lang": lang},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            idempotency_status, pending = self._idempotency.begin(
                request_id, payload_hash
            )
            if idempotency_status == "conflict":
                return 409, {
                    "v": PROTOCOL_VERSION, "request_id": request_id,
                    "status": "error", "error": "idempotency_conflict",
                }
            if idempotency_status == "duplicate":
                return 200, {
                    "v": PROTOCOL_VERSION, "request_id": request_id,
                    "status": "duplicate", "queue_depth": None,
                }
            if idempotency_status == "pending":
                if pending and pending.wait(12) and self._idempotency.contains(request_id):
                    return 200, {
                        "v": PROTOCOL_VERSION, "request_id": request_id,
                        "status": "duplicate", "queue_depth": None,
                    }
                return 503, {
                    "v": PROTOCOL_VERSION, "request_id": request_id,
                    "status": "error", "error": "unavailable",
                }
            reserved = True
            response = self._submit(request)
        except ProactiveSpeechError as error:
            if reserved:
                self._idempotency.finish(request_id, keep=False)
            reason = str(error)
            if reason in {
                "speech ingress is not configured",
                "could not reach the local OVOS message bus",
                "OVOS did not acknowledge the request",
                "unavailable",
            }:
                status, public_reason = 503, "unavailable"
            elif reason == "too_large":
                status, public_reason = 413, "too_large"
            else:
                status, public_reason = 400, "malformed"
            return status, {
                "v": PROTOCOL_VERSION, "request_id": request_id,
                "status": "error", "error": public_reason,
            }
        except OSError:
            if reserved:
                self._idempotency.finish(request_id, keep=False)
            return 503, {
                "v": PROTOCOL_VERSION, "request_id": request_id,
                "status": "error", "error": "unavailable",
            }
        except (TypeError, ValueError):
            if reserved:
                self._idempotency.finish(request_id, keep=False)
            return 400, {"v": PROTOCOL_VERSION, "status": "error", "error": "malformed"}

        status_name = response.get("status")
        try:
            self._idempotency.finish(
                request_id, keep=status_name in {"accepted", "duplicate"}
            )
        except OSError:
            return 503, {
                "v": PROTOCOL_VERSION, "request_id": request_id,
                "status": "error", "error": "unavailable",
            }
        result = {
            "v": PROTOCOL_VERSION,
            "request_id": request_id,
            "status": status_name,
            "queue_depth": response.get("queue_depth"),
        }
        if status_name == "accepted":
            return 202, result
        if status_name == "duplicate":
            return 200, result
        reason = str(response.get("reason") or "rejected")
        status = 429 if reason in {"rate_limited", "queue_full"} else 503 if reason == "unavailable" else 400
        result["error"] = reason
        return status, result


class SpeechHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = MAX_CONCURRENT_REQUESTS

    def __init__(self, address: tuple[str, int], app: SpeechHTTPApplication):
        self.app = app
        self._request_slots = BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
        super().__init__(address, SpeechHTTPRequestHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._request_slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.0 503 Service Unavailable\r\n"
                    b"Connection: close\r\nContent-Length: 0\r\n\r\n"
                )
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class SpeechHTTPRequestHandler(BaseHTTPRequestHandler):
    server: SpeechHTTPServer
    server_version = "OVOSSpeechIngress/1"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)
        self._deadline = Timer(REQUEST_DEADLINE_SECONDS, self._expire_request)
        self._deadline.daemon = True
        self._deadline.start()

    def finish(self) -> None:
        self._deadline.cancel()
        super().finish()

    def _expire_request(self) -> None:
        try:
            self.connection.shutdown(2)
        except OSError:
            pass

    def _serve(self) -> None:
        path = self.path.split("?", 1)[0]
        headers = {key: value for key, value in self.headers.items()}
        if self.command != "POST" or path not in SPEAK_PATHS:
            status, response = self.server.app.handle(
                self.command, path, headers, b""
            )
        elif not self.server.app.is_authorized(headers):
            status, response = self.server.app.handle(
                self.command, path, headers, b""
            )
        else:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length < 0:
                status, response = 400, {
                    "v": PROTOCOL_VERSION, "status": "error", "error": "malformed"
                }
            elif length > MAX_HTTP_BODY_BYTES:
                status, response = 413, {
                    "v": PROTOCOL_VERSION, "status": "error", "error": "too_large"
                }
            else:
                body = self.rfile.read(length) if length else b""
                status, response = self.server.app.handle(
                    self.command, path, headers, body
                )
        encoded = json.dumps(response, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        self._serve()

    def do_POST(self) -> None:
        self._serve()

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.info("speech ingress request: " + format, *args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ovos-openclaw-http",
        description="Serve authenticated HTTP ingress for proactive OVOS speech.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--allow-non-loopback", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("port must be between 1 and 65535")
    if args.host not in {"127.0.0.1", "localhost"} and not args.allow_non_loopback:
        raise SystemExit("refusing non-loopback bind without --allow-non-loopback")
    token = get_speech_http_token() or ""
    speech_secret = get_proactive_speech_secret() or ""
    if len(token) < 32 or len(speech_secret) < 32:
        raise SystemExit("speech HTTP ingress is not configured")
    try:
        idempotency = HTTPIdempotencyStore(
            Path.home()
            / ".local/state/ovos-openclaw-skill/http-idempotency.json"
        )
    except (OSError, ValueError) as error:
        raise SystemExit("speech HTTP ingress state is unavailable") from error
    app = SpeechHTTPApplication(
        token_provider=lambda: token,
        speech_secret_provider=lambda: speech_secret,
        idempotency=idempotency,
        default_lang=str(Configuration().get("lang", "en-us")),
    )
    server = SpeechHTTPServer((args.host, args.port), app)
    LOGGER.warning("speech ingress listening on http://%s:%s", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
