"""OVOS skill routing spoken requests to OpenClaw."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ovos_bus_client import Message
from ovos_utils import classproperty
from ovos_utils.process_utils import RuntimeRequirements
from ovos_workshop.decorators import fallback_handler, intent_handler
from ovos_workshop.skills.fallback import FallbackSkill

from .client import OpenClawClient, OpenClawError
from .credentials import get_gateway_token
from .proactive import ProactiveSpeechManager, REQUEST_TOPIC, RESULT_TOPIC


DEFAULT_GATEWAY_URL = "http://127.0.0.1:18789"
DEFAULT_MODEL = "openclaw/default"
DEFAULT_CONVERSATION = "ovos-openclaw-skill"
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_TOKENS = 180
DEFAULT_MAX_FOLLOW_UPS = 25
DEFAULT_SPEECH_WAIT_TIMEOUT = 60
RECORD_END_TOPIC = "recognizer_loop:record_end"
PLAY_SOUND_TOPIC = "mycroft.audio.play_sound"
END_LISTENING_SOUND = str(
    Path(__file__).parent / "res" / "snd" / "end_listening.wav"
)


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

    def initialize(self) -> None:
        """Start the authenticated proactive-speech queue."""
        settings = getattr(self, "settings", {})
        self._proactive_speech = ProactiveSpeechManager(
            speak=self._speak_proactively,
            result=self._emit_proactive_result,
            quiet_start=str(settings.get("proactive_quiet_start", "23:00")),
            quiet_end=str(settings.get("proactive_quiet_end", "08:00")),
        )
        self._proactive_speech.start()
        self.add_event(
            REQUEST_TOPIC,
            self._handle_proactive_speech,
            speak_errors=False,
        )
        self.add_event(
            RECORD_END_TOPIC,
            self._handle_record_end,
            speak_errors=False,
        )

    def shutdown(self) -> None:
        """Stop accepting announcements and drain no stale speech on reload."""
        manager = getattr(self, "_proactive_speech", None)
        if manager:
            manager.stop()

    def _handle_proactive_speech(self, message: Message) -> None:
        response = self._proactive_speech.submit(message.data)
        self.bus.emit(message.response(response))

    def _handle_record_end(self, message: Message) -> None:
        """Play a brief cue when OVOS has finished capturing an utterance."""
        self.bus.emit(
            message.forward(
                PLAY_SOUND_TOPIC,
                {"uri": END_LISTENING_SOUND},
            )
        )

    def _speak_proactively(self, text: str, lang: str) -> None:
        # The skill locale owns TTS language. The signed language field keeps
        # the protocol explicit and leaves room for multilingual support later.
        self.speak(
            text,
            expect_response=True,
            wait=45,
            meta={"proactive": True, "requested_lang": lang},
        )

    def _emit_proactive_result(self, result: dict[str, Any]) -> None:
        self.bus.emit(
            Message(
                RESULT_TOPIC,
                result,
                context={"skill_id": self.skill_id},
            )
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
        self._conversation(query, message)

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
        self._conversation(query, message)
        return True

    def _conversation(self, query: str, message: Any) -> None:
        """Answer a request, then collect bounded no-wake follow-up turns."""
        follow_up_enabled = bool(
            getattr(self, "settings", {}).get("follow_up_enabled", True)
        )
        max_follow_ups = self._bounded_int(
            "follow_up_max_turns", DEFAULT_MAX_FOLLOW_UPS, 0, 25
        )

        for turn in range(max_follow_ups + 1):
            should_listen = follow_up_enabled and turn < max_follow_ups
            if not self._answer(query, wait_for_speech=should_listen):
                return
            if not should_listen:
                return

            follow_up = self.get_response(message=message, num_retries=0)
            if not follow_up or self._is_blank_audio(follow_up):
                return
            query = str(follow_up).strip()

    def _answer(self, query: str, wait_for_speech: bool = False) -> bool:
        token = get_gateway_token()
        if not token:
            self.speak("Dravon is not configured yet.")
            return False

        try:
            answer = self._make_client(token).complete(query)
        except OpenClawError as error:
            # OpenClawError messages are deliberately coarse and never contain tokens.
            self.log.warning("OpenClaw bridge request failed: %s", error)
            self.speak("I couldn't reach Dravon right now.")
            return False

        if wait_for_speech:
            wait_timeout = self._bounded_int(
                "follow_up_speech_timeout",
                DEFAULT_SPEECH_WAIT_TIMEOUT,
                5,
                120,
            )
            self.speak(answer, wait=wait_timeout)
        else:
            self.speak(answer)
        return True

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

