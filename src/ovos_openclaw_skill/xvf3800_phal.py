"""Animated OVOS LED cues for the reSpeaker XVF3800 USB 4-Mic Array.

Firmware 2.0.7+ supports independent colors for the 12 LEDs. All writes are
volatile; no SAVE_CONFIGURATION or microphone mute controls are touched.
"""

import math
import time
from threading import Event, Lock, Thread

import usb.core
import usb.util
from ovos_plugin_manager.templates.phal import PHALPlugin

VID, PID = 0x2886, 0x001A
LED_RESID = 20
LED_EFFECT = 12
LED_RING_COLOR = 19
LED_COLOR = 16
LED_DOA_COLOR = 17
DOA_VALUE = 18
DOA_BASE_COLOR = 0x0000FF
DOA_DIRECTION_COLOR = 0x00FF00
LED_COUNT = 12
FRAME_INTERVAL = 0.10


def _color(red, green, blue):
    return (max(0, min(255, int(red))) << 16) | (max(0, min(255, int(green))) << 8) | max(0, min(255, int(blue)))


def render_ring(state, seconds):
    """Return a frame for thinking (amber) or speaking (green)."""
    if state == "thinking":
        # A distinct, faster amber comet.
        head = int(seconds * 8) % LED_COUNT
        return [_color(28 + 190 * max(0, 1 - ((head - i) % LED_COUNT) / 4),
                       10 + 95 * max(0, 1 - ((head - i) % LED_COUNT) / 4), 0)
                for i in range(LED_COUNT)]
    if state == "speaking":
        # Soft green wave, visually different from the two rotating indicators.
        return [_color(0, 35 + 130 * (0.5 + 0.5 * math.sin(seconds * 7 - i * 0.8)),
                       8 + 24 * (0.5 + 0.5 * math.sin(seconds * 7 - i * 0.8)))
                for i in range(LED_COUNT)]
    raise ValueError("invalid LED state")


class XVF3800LED:
    def __init__(self, on_error=None):
        self._lock = Lock()
        self._device = None
        self._state = "idle"
        self._mode = None
        self._doa_ready = False
        self._quiet_samples = 0
        self._wake = Event()
        self._stop = Event()
        self._thread = None
        self._on_error = on_error

    def _write(self, command, payload):
        self._device.ctrl_transfer(
            usb.util.CTRL_OUT | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE,
            0, command, LED_RESID, payload, timeout=1000,
        )

    def _speech_detected(self):
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

    def set_state(self, state):
        if state not in ("idle", "listening", "thinking", "speaking"):
            raise ValueError("invalid LED state")
        with self._lock:
            if self._state != state:
                self._doa_ready = False
                self._quiet_samples = 0
            self._state = state
        self._wake.set()

    def tick(self, seconds=None):
        """Write one frame. Kept separate from the worker for deterministic tests."""
        if seconds is None:
            seconds = time.monotonic()
        with self._lock:
            state = self._state
            if self._device is None:
                self._device = usb.core.find(idVendor=VID, idProduct=PID)
                self._mode = None
                self._doa_ready = False
            if self._device is None:
                raise OSError("XVF3800 USB device unavailable")
            try:
                if state == "idle":
                    if self._mode != "idle":
                        self._write(LED_EFFECT, b"\x00")
                        self._mode = "idle"
                elif state == "listening":
                    # Preserve the existing beam-direction cue. The blue fill
                    # covers the firmware's blank ring during quiet gaps.
                    if not self._doa_ready:
                        self._write(LED_DOA_COLOR, b"".join(c.to_bytes(4, "little")
                            for c in (DOA_BASE_COLOR, DOA_DIRECTION_COLOR)))
                        self._write(LED_COLOR, DOA_BASE_COLOR.to_bytes(4, "little"))
                        self._doa_ready = True
                    if self._speech_detected():
                        self._quiet_samples = 0
                        if self._mode != "doa":
                            self._write(LED_EFFECT, b"\x04")
                            self._mode = "doa"
                    else:
                        self._quiet_samples += 1
                        if self._mode not in ("solid", "doa") or self._quiet_samples >= 4:
                            if self._mode != "solid":
                                self._write(LED_EFFECT, b"\x03")
                                self._mode = "solid"
                else:
                    frame = render_ring(state, seconds)
                    self._write(LED_RING_COLOR, b"".join(c.to_bytes(4, "little") for c in frame))
                    if self._mode != "ring":
                        self._write(LED_EFFECT, b"\x05")
                        self._mode = "ring"
            except Exception:
                usb.util.dispose_resources(self._device)
                self._device = None
                self._mode = None
                self._doa_ready = False
                raise

    def _run(self):
        next_error_log = 0
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                if self._on_error and time.monotonic() >= next_error_log:
                    self._on_error(exc)
                    next_error_log = time.monotonic() + 30
                self._wake.wait(1.0)
            else:
                next_error_log = 0
                self._wake.wait(FRAME_INTERVAL)
            self._wake.clear()

    def start(self):
        self._thread = Thread(target=self._run, name="xvf3800-led", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.set_state("idle")
        self.tick()


class XVF3800PHAL(PHALPlugin):
    def __init__(self, bus=None, name="xvf3800-led", config=None):
        self.led = XVF3800LED(on_error=self._log_led_error)
        super().__init__(bus=bus, name=name, config=config)
        self.led.start()

    def register_core_events(self):
        super().register_core_events()
        self._on("recognizer_loop:speech.recognition.unknown", self.on_unknown_speech)

    def _log_led_error(self, exc):
        self.log.warning("XVF3800 LED unavailable: %s", type(exc).__name__)

    def _set_state(self, state):
        self.led.set_state(state)

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
        try:
            self.led.stop()
        except Exception as exc:
            self._log_led_error(exc)
        super().shutdown()
