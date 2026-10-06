"""OVOS skill routing spoken requests to OpenClaw."""

from __future__ import annotations

from typing import Any

from ovos_utils import classproperty
from ovos_utils.process_utils import RuntimeRequirements
from ovos_workshop.decorators import fallback_handler, intent_handler
from ovos_workshop.skills.fallback import FallbackSkill

from .client import OpenClawClient, OpenClawError
from .credentials import get_gateway_token


DEFAULT_GATEWAY_URL = "http://127.0.0.1:18789"
DEFAULT_MODEL = "openclaw/default"
DEFAULT_CONVERSATION = "ovos-openclaw-skill"
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_TOKENS = 180


class OpenClawSkill(FallbackSkill):
    """Forward explicit or direct post-wake requests to the local Gateway."""

    @classproperty
    def runtime_requirements(cls) -> RuntimeRequirements:
        return RuntimeRequirements(
            internet_before_load=False,
            network_before_load=False,
            gui_before_load=False,
            requires_internet=False,
            requires_network=False,
            requires_gui=False,
            no_internet_fallback=True,
            no_network_fallback=True,
            no_gui_fallback=True,
        )

    def can_answer(self, message: Any) -> bool:
        """Advertise direct routing for non-empty captured utterances."""
        enabled = bool(getattr(self, "settings", {}).get("direct_route", True))
        return enabled and bool(self._fallback_query(message))

    @intent_handler("dravon.intent")
    def handle_dravon(self, message: Any) -> None:
        query = str(message.data.get("query", "")).strip()
        if not query:
            self.speak("What would you like to ask Dravon?")
            return
        self._answer(query)

    @fallback_handler(priority=1)
    def handle_direct_request(self, message: Any) -> bool:
        """Consume every post-wake utterance when direct routing is enabled."""
        if not self.can_answer(message):
            return False
        query = self._fallback_query(message)
        if self._is_blank_audio(query):
            # Whisper.cpp emits this marker for silence. Consume it without
            # letting another fallback speak a confusing answer.
            return True
        self._answer(query)
        return True

    def _answer(self, query: str) -> None:
        token = get_gateway_token()
        if not token:
            self.speak("Dravon is not configured yet.")
            return

        try:
            answer = self._make_client(token).complete(query)
        except OpenClawError as error:
            # OpenClawError messages are deliberately coarse and never contain tokens.
            self.log.warning("OpenClaw bridge request failed: %s", error)
            self.speak("I couldn't reach Dravon right now.")
            return
        self.speak(answer)

    @staticmethod
    def _fallback_query(message: Any) -> str:
        query = message.data.get("utterance")
        if not query:
            utterances = message.data.get("utterances") or []
            query = utterances[0] if utterances else ""
        return str(query).strip()

    @staticmethod
    def _is_blank_audio(query: str) -> bool:
        normalized = query.lower().replace("_", " ").strip(" []")
        normalized = " ".join(normalized.split())
        return not normalized or normalized == "blank audio"

    def _make_client(self, token: str) -> OpenClawClient:
        return OpenClawClient(
            base_url=str(self.settings.get("gateway_url", DEFAULT_GATEWAY_URL)),
            token=token,
            model=str(self.settings.get("model", DEFAULT_MODEL)),
            conversation=str(
                self.settings.get("conversation", DEFAULT_CONVERSATION)
            ),
            timeout=self._bounded_float("timeout", DEFAULT_TIMEOUT, 1.0, 120.0),
            max_tokens=self._bounded_int(
                "max_tokens", DEFAULT_MAX_TOKENS, 16, 512
            ),
        )

    def _bounded_float(
        self, key: str, default: float, minimum: float, maximum: float
    ) -> float:
        try:
            value = float(self.settings.get(key, default))
        except (TypeError, ValueError):
            return default
        return min(max(value, minimum), maximum)

    def _bounded_int(
        self, key: str, default: int, minimum: int, maximum: int
    ) -> int:
        try:
            value = int(self.settings.get(key, default))
        except (TypeError, ValueError):
            return default
        return min(max(value, minimum), maximum)


def create_skill() -> OpenClawSkill:
    """Compatibility factory for older OVOS skill loaders."""
    return OpenClawSkill()


__all__ = ["OpenClawSkill", "create_skill"]

