from unittest.mock import Mock, patch

import usb.util

from ovos_openclaw_skill.xvf3800_phal import XVF3800LED, XVF3800PHAL


def test_usb_led_commands_do_not_persist_or_touch_mute():
    device = Mock()
    led = XVF3800LED()
    with patch("ovos_openclaw_skill.xvf3800_phal.usb.core.find", return_value=device), \
         patch("ovos_openclaw_skill.xvf3800_phal.Thread"):
        led.set_state("listening")
        led.set_state("thinking")
        led.set_state("speaking")
        led.set_state("idle")
    calls = device.ctrl_transfer.call_args_list
    assert [call.args[2] for call in calls] == [17, 16, 12, 16, 16, 12]
    assert [call.args[4] for call in calls] == [
        b"\xff\x00\x00\x00\x00\xff\x00\x00",
        b"\xff\x00\x00\x00", b"\x03",
        b"\x00\xbf\xff\x00",
        b"\x00\xff\x00\x00", b"\x00",
    ]
    assert all(call.args[3] == 20 for call in calls)


def test_event_mapping_and_failure_isolation():
    plugin = object.__new__(XVF3800PHAL)
    plugin.led = Mock()
    plugin.log = Mock()
    plugin.on_record_begin()
    plugin.on_record_end()
    plugin.on_audio_output_start()
    plugin.on_audio_output_end()
    plugin.on_record_begin()
    plugin.on_record_end()
    plugin.on_unknown_speech()
    assert [call.args[0] for call in plugin.led.set_state.call_args_list] == [
        "listening", "thinking", "speaking", "idle",
        "listening", "thinking", "idle",
    ]
    plugin.led.set_state.side_effect = OSError("private USB error")
    plugin.on_record_begin()
    assert plugin.log.warning.called


def test_unknown_speech_event_is_registered():
    plugin = object.__new__(XVF3800PHAL)
    plugin._on = Mock()
    with patch("ovos_openclaw_skill.xvf3800_phal.PHALPlugin.register_core_events"):
        plugin.register_core_events()
    plugin._on.assert_called_once_with(
        "recognizer_loop:speech.recognition.unknown", plugin.on_unknown_speech,
    )


def test_speech_poll_switches_between_direction_and_solid_blue():
    device = Mock()
    led = XVF3800LED()
    with patch("ovos_openclaw_skill.xvf3800_phal.usb.core.find", return_value=device), \
         patch("ovos_openclaw_skill.xvf3800_phal.Thread"):
        led.set_state("listening")
    # Status 0, azimuth 90 degrees, then the speech-detected flag.
    replies = iter([bytes((0, 90, 0, 1, 0)), bytes((0, 90, 0, 0, 0))])
    device.ctrl_transfer.side_effect = lambda request_type, *_args, **_kwargs: (
        next(replies) if request_type & usb.util.CTRL_IN else None
    )

    class TwoPolls:
        def __init__(self):
            self.count = 0

        def wait(self, _interval):
            self.count += 1
            return self.count > 2

        def is_set(self):
            return False

    led._poll_listening(TwoPolls())
    effect_writes = [call.args[4] for call in device.ctrl_transfer.call_args_list
                     if call.args[0] & usb.util.CTRL_IN == 0 and call.args[2] == 12]
    assert effect_writes == [b"\x03", b"\x04", b"\x03"]
