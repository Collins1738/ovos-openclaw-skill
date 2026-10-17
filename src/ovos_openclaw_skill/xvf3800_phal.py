"""OVOS PHAL LED cues for the Seeed ReSpeaker XVF3800 USB 4-Mic Array.

Only volatile LED registers are touched. In particular, this never calls
SAVE_CONFIGURATION or the GPO mute pins.
"""

import logging
from threading import Event, Lock, Thread

import usb.core
import usb.util
from ovos_plugin_manager.templates.phal import PHALPlugin

VID, PID = 0x2886, 0x001A
LED_RESID = 20
LED_EFFECT = 12
LED_COLOR = 16
LED_DOA_COLOR = 17
DOA_VALUE = 18
DOA_BASE_COLOR = 0x0000FF
DOA_DIRECTION_COLOR = 0x00FF00
COLORS = {"thinking": 0xFFBF00, "speaking": 0x00FF00}
LOG = logging.getLogger(__name__)


class XVF3800LED:
    """Small, device-specific subset of Seeed's xvf_host USB control protocol."""

    def __init__(self):
        self._lock = Lock()
        self._device = None
        self._state = "idle"
        self._effect = None
        self._poll_stop = None

    def _write(self, command, payload):
        self._device.ctrl_transfer(
            usb.util.CTRL_OUT | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE,
            0, command, LED_RESID, payload, timeout=1000,
        )

    def _speech_detected(self):
        # Seeed's DOA_VALUE returns status, azimuth (uint16), speech (uint16).
        for _ in range(3):
            response = self._device.ctrl_transfer(
                usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE,
                0, 0x80 | DOA_VALUE, LED_RESID, 5, timeout=1000,
            )
            if response[0] == 0:
                return bool(int.from_bytes(bytes(response[3:5]), "little"))
            if response[0] != 64:
                raise OSError(f"XVF3800 DOA read status {response[0]}")
        raise OSError("XVF3800 DOA read remained busy")

    def _set_effect(self, effect):
        if self._effect != effect:
            self._write(LED_EFFECT, bytes((effect,)))
            self._effect = effect

    def _poll_listening(self, stop):
        # A single low VAD sample during speech is common. Flipping effect 4
        # off on that sample prevents the firmware DoA indicator from settling.
        quiet_samples = 0
        while not stop.wait(0.08):
            with self._lock:
                if self._state != "listening" or stop.is_set():
                    return
                try:
                    # Firmware DoA mode blanks the whole ring when speech drops.
                    # Solid blue fills that quiet interval until recording ends.
                    if self._speech_detected():
                        quiet_samples = 0
                        self._set_effect(4)
                    else:
                        quiet_samples += 1
                        if quiet_samples >= 4:
                            self._set_effect(3)
                except Exception as exc:
                    LOG.warning("XVF3800 direction polling stopped: %s", type(exc).__name__)
                    usb.util.dispose_resources(self._device)
                    self._device = None
                    self._effect = None
                    return

    def set_state(self, state):
        if state not in ("idle", "listening") and state not in COLORS:
            raise ValueError("invalid LED state")
        with self._lock:
            if self._poll_stop is not None:
                self._poll_stop.set()
                self._poll_stop = None
            self._state = state
            if self._device is None:
                self._device = usb.core.find(idVendor=VID, idProduct=PID)
            if self._device is None:
                raise OSError("XVF3800 USB device unavailable")
            try:
                if state == "idle":
                    self._set_effect(0)
                elif state == "listening":
                    # Firmware tracks the beam but blanks the ring between speech bursts.
                    colors = (DOA_BASE_COLOR, DOA_DIRECTION_COLOR)
                    self._write(
                        LED_DOA_COLOR,
                        b"".join(color.to_bytes(4, "little") for color in colors),
                    )
                    self._write(LED_COLOR, DOA_BASE_COLOR.to_bytes(4, "little"))
                    self._set_effect(3)
                    stop = Event()
                    self._poll_stop = stop
                    Thread(target=self._poll_listening, args=(stop,), daemon=True).start()
                else:
                    self._write(LED_COLOR, COLORS[state].to_bytes(4, "little"))
                    self._set_effect(3)
            except Exception:
                usb.util.dispose_resources(self._device)
                self._device = None
                self._effect = None
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
