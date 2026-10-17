from unittest.mock import Mock, patch

from ovos_openclaw_skill.xvf3800_phal import (
    LED_COUNT, XVF3800LED, XVF3800PHAL, render_listening_ring, render_ring,
)


def test_ring_frames_and_usb_commands_never_persist_or_touch_mute():
    device = Mock()
    device.ctrl_transfer.side_effect = lambda *args, **kwargs: bytes([0, 0, 0, 1, 0]) if args[0] & 0x80 else None
    led = XVF3800LED()
    with patch("ovos_openclaw_skill.xvf3800_phal.usb.core.find", return_value=device):
        for state in ("listening", "thinking", "speaking"):
            led.set_state(state)
            led.tick(1.0)
        led.set_state("idle")
        led.tick(1.0)
    calls = device.ctrl_transfer.call_args_list
    assert [call.args[2] for call in calls] == [146, 19, 12, 19, 19, 12]
    assert all(call.args[3] == 20 for call in calls)
    assert all(len(call.args[4]) == LED_COUNT * 4 for call in calls if call.args[2] == 19)
    assert calls[2].args[4] == b"\x05"
    assert calls[-1].args[4] == b"\x00"
    assert len(set(render_ring("thinking", 0))) > 1
    assert render_ring("thinking", 0) != render_ring("speaking", 0)


def test_event_mapping():
    plugin = object.__new__(XVF3800PHAL)
    plugin.led = Mock()
    plugin.on_record_begin()
    plugin.on_record_end()
    plugin.on_audio_output_start()
    plugin.on_audio_output_end()
    plugin.on_unknown_speech()
    assert [call.args[0] for call in plugin.led.set_state.call_args_list] == [
        "listening", "thinking", "speaking", "idle", "idle",
    ]


def test_unknown_speech_event_is_registered():
    plugin = object.__new__(XVF3800PHAL)
    plugin._on = Mock()
    with patch("ovos_openclaw_skill.xvf3800_phal.PHALPlugin.register_core_events"):
        plugin.register_core_events()
    plugin._on.assert_called_once_with(
        "recognizer_loop:speech.recognition.unknown", plugin.on_unknown_speech,
    )


def test_listening_stays_blue_and_green_direction_fades_without_mode_flicker():
    device = Mock()
    speech = [True] + [False] * 8 + [True]

    def transfer(*args, **kwargs):
        if args[0] & 0x80:
            return bytes([0, 90, 0, int(speech.pop(0)), 0])
        return None

    device.ctrl_transfer.side_effect = transfer
    led = XVF3800LED()
    with patch("ovos_openclaw_skill.xvf3800_phal.usb.core.find", return_value=device):
        led.set_state("listening")
        for _ in range(10):
            led.tick(1.0)
    calls = device.ctrl_transfer.call_args_list
    effects = [call.args[4] for call in calls if call.args[2] == 12]
    assert effects == [b"\x05"]
    frames = [call.args[4] for call in calls if call.args[2] == 19]
    assert len(frames) == 10
    assert frames[0] == frames[-1]
    assert frames[0] != frames[8]
    assert frames[8] == b"\xff\x00\x00\x00" * LED_COUNT
    assert all(frame for frame in frames)
    assert render_listening_ring(90, 1) != render_listening_ring(0, 1)
