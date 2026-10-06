from importlib.metadata import entry_points
from importlib.resources import files

from padacioso import IntentContainer

from ovos_openclaw_skill import OpenClawSkill
from ovos_openclaw_skill.proactive import main as proactive_speech_main


def test_plugin_entry_point_loads_skill_class():
    matches = entry_points(group="opm.skill", name="ovos-openclaw-skill")
    matches = tuple(matches)
    assert len(matches) == 1
    assert matches[0].load() is OpenClawSkill


def test_proactive_speech_console_entry_point_loads():
    matches = entry_points(group="console_scripts", name="ovos-openclaw-speak")
    matches = tuple(matches)
    assert len(matches) == 1
    assert matches[0].load() is proactive_speech_main


def test_intent_resource_is_packaged():
    text = (
        files("ovos_openclaw_skill")
        .joinpath("locale/en-US/dravon.intent")
        .read_text(encoding="utf-8")
    )
    assert "ask Dravon {query}" in text


def test_explicit_phrases_match_and_extract_query():
    lines = [
        line
        for line in files("ovos_openclaw_skill")
        .joinpath("locale/en-US/dravon.intent")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    matcher = IntentContainer()
    matcher.add_intent("dravon", lines)

    examples = {
        "ask Dravon what time is it": "what time is it",
        "tell Dravon make a note": "make a note",
        "Dravon hello there": "hello there",
        "ask Draven what time is it": "what time is it",
        "tell Dravan make a note": "make a note",
    }
    for utterance, expected_query in examples.items():
        match = matcher.calc_intent(utterance)
        assert match["name"] == "dravon"
        assert match["entities"]["query"] == expected_query

