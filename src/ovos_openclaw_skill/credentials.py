"""Credential lookup that deliberately avoids OVOS settings storage."""

from __future__ import annotations

import os
import subprocess
import sys


ENVIRONMENT_VARIABLE = "OPENCLAW_GATEWAY_TOKEN"
KEYCHAIN_SERVICE = "ovos-openclaw-skill"
KEYCHAIN_ACCOUNT = "gateway-token"


def get_gateway_token() -> str | None:
    """Return the token from the environment, then macOS Keychain."""
    token = os.environ.get(ENVIRONMENT_VARIABLE, "").strip()
    if token:
        return token
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
                KEYCHAIN_ACCOUNT,
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

