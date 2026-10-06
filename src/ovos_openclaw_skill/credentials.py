"""Credential lookup that deliberately avoids OVOS settings storage."""

from __future__ import annotations

import os
import subprocess
import sys


GATEWAY_ENVIRONMENT_VARIABLE = "OPENCLAW_GATEWAY_TOKEN"
PROACTIVE_SPEECH_ENVIRONMENT_VARIABLE = "OVOS_OPENCLAW_SPEECH_SECRET"
SPEECH_HTTP_ENVIRONMENT_VARIABLE = "OVOS_OPENCLAW_HTTP_TOKEN"
KEYCHAIN_SERVICE = "ovos-openclaw-skill"
GATEWAY_KEYCHAIN_ACCOUNT = "gateway-token"
PROACTIVE_SPEECH_KEYCHAIN_ACCOUNT = "proactive-speech-key"
SPEECH_HTTP_KEYCHAIN_ACCOUNT = "speech-http-token"


def _get_credential(environment_variable: str, keychain_account: str) -> str | None:
    value = os.environ.get(environment_variable, "").strip()
    if value:
        return value
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                keychain_account,
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def get_gateway_token() -> str | None:
    """Return the Gateway token from the environment, then macOS Keychain."""
    return _get_credential(
        GATEWAY_ENVIRONMENT_VARIABLE, GATEWAY_KEYCHAIN_ACCOUNT
    )


def get_proactive_speech_secret() -> str | None:
    """Return the independent proactive-speech HMAC secret."""
    return _get_credential(
        PROACTIVE_SPEECH_ENVIRONMENT_VARIABLE,
        PROACTIVE_SPEECH_KEYCHAIN_ACCOUNT,
    )


def get_speech_http_token() -> str | None:
    """Return the independent bearer token for HTTP speech ingress."""
    return _get_credential(
        SPEECH_HTTP_ENVIRONMENT_VARIABLE,
        SPEECH_HTTP_KEYCHAIN_ACCOUNT,
    )

