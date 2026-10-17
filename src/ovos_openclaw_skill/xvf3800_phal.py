"""OVOS PHAL LED cues for the Seeed ReSpeaker XVF3800 USB 4-Mic Array.

Only volatile LED registers are touched. In particular, this never calls
SAVE_CONFIGURATION or the GPO mute pins.
"""

from threading import Lock

import usb.core
import usb.util
from ovos_plugin_manager.templates.phal import PHALPlugin

VID, PID = 0x2886, 0x001A
LED_RESID = 20
LED_EFFECT = 12
LED_COLOR = 16
LED_DOA_COLOR = 17
DOA_BASE_COLOR = 0x0000FF
DOA_DIRECTION_COLOR = 0x00FF00
COLORS = {"thinking": 0xFFBF00, "speaking": 0x00FF00}


class XVF3800LED:
    """Small, device-specific subset of Seeed's xvf_host USB control protocol."""

    def __init__(self):
        self._lock = Lock()
        self._device = None

    def _write(self, command, payload):
        self._device.ctrl_transfer(
            usb.util.CTRL_OUT | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE,
            0, command, LED_RESID, payload, timeout=1000,
        )

    def set_state(self, state):
        if state not in ("idle", "listening") and state not in COLORS:
            raise ValueError("invalid LED state")
        with self._lock:
            if self._device is None:
                self._device = usb.core.find(idVendor=VID, idProduct=PID)
            if self._device is None:
                raise OSError("XVF3800 USB device unavailable")
            try:
                if state == "idle":
                    self._write(LED_EFFECT, b"\x00")
                elif state == "listening":
                    # Firmware tracks the active beam itself; no host polling is needed.
                    colors = (DOA_BASE_COLOR, DOA_DIRECTION_COLOR)
                    self._write(
                        LED_DOA_COLOR,
                        b"".join(color.to_bytes(4, "little") for color in colors),
                    )
                    self._write(LED_EFFECT, b"\x04")
                else:
                    self._write(LED_COLOR, COLORS[state].to_bytes(4, "little"))
                    self._write(LED_EFFECT, b"\x03")
            except Exception:
                usb.util.dispose_resources(self._device)
                self._device = None
                raise


class XVF3800PHAL(PHALPlugin):
    def __init__(self, bus=None, name="xvf3800-led", config=None):
        self.led = XVF3800LED()
        super().__init__(bus=bus, name=name, config=config)
        self._set_state("idle")

    def register_core_events(self):
        super().register_core_events()
        # A silent command still emits record_end, but no speech output follows.
        # The listener reports that path explicitly after the empty transcript.
        self._on("recognizer_loop:speech.recognition.unknown", self.on_unknown_speech)

    def _set_state(self, state):
        try:
            self.led.set_state(state)
        except Exception as exc:
            self.log.warning("XVF3800 LED %s unavailable: %s", state, type(exc).__name__)

    def on_record_begin(self, message=None):
        self._set_state("listening")

    def on_record_end(self, message=None):
        self._set_state("thinking")

    def on_audio_output_start(self, message=None):
        self._set_state("speaking")

    def on_audio_output_end(self, message=None):
        self._set_state("idle")

    def on_unknown_speech(self, message=None):
        self._set_state("idle")

    def shutdown(self):
        self._set_state("idle")
        super().shutdown()
