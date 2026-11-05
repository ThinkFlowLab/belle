from dataclasses import replace

import pytest
from conformance_kit import ModelTurn, RecordedBackend, World
from scripted_backend import ScriptedBackend

from belle_agent.backend import EVENT_TOOL_CALLED, EVENT_TOOL_RESULT
from belle_agent.config import load_config
from belle_agent.host import host_from_config
from belle_agent.platforms.base import affordances

OPTIONAL_REPLY_IN_C1 = """platforms: {loopback: {enabled: true}}
scopes:
  - match: {channel: loopback, chat: C1}
    delivery: {mode: [all], reply: optional}
"""


@pytest.fixture
async def world(tmp_path):
    backend = ScriptedBackend()
    path = tmp_path / "config.yaml"
    path.write_text(OPTIONAL_REPLY_IN_C1, encoding="utf-8")
    hosted = host_from_config(load_config(path), backend, state_dir=tmp_path / "state")
    await hosted.start()
    try:
        yield World(hosted, hosted.platform, backend, RecordedBackend(backend))
    finally:
        await hosted.stop()


def _declare(world, **fields):
    world.platform.affordances = replace(affordances(world.platform), **fields)


def _tool_events(world):
    return [event.kind for _, event in world.platform.progressed if event.kind in (EVENT_TOOL_CALLED, EVENT_TOOL_RESULT)]


async def test_a_card_that_waits_for_a_post_holds_the_tool_events_of_a_silent_turn(world):
    world.platform.cards_wait_for_a_post = True
    world.kit.play(ModelTurn(final="nobody asked\n", calls=(("react_to_message", {"emoji": "eyes"}),)))

    await world.send(world.message("just talking"))

    assert _tool_events(world) == []


async def test_without_it_the_tool_events_of_a_silent_turn_are_handed_over(world):
    world.kit.play(ModelTurn(final="nobody asked\n", calls=(("react_to_message", {"emoji": "eyes"}),)))

    await world.send(world.message("just talking"))

    assert _tool_events(world) == [EVENT_TOOL_CALLED, EVENT_TOOL_RESULT]


async def test_a_declared_table_cannot_say_otherwise_than_the_attribute(world):
    _declare(world, cards_wait_for_a_post=True)
    world.kit.play(ModelTurn(final="nobody asked\n", calls=(("react_to_message", {"emoji": "eyes"}),)))

    await world.send(world.message("just talking"))

    assert _tool_events(world) == [EVENT_TOOL_CALLED, EVENT_TOOL_RESULT]


async def test_startup_reports_an_affordance_contradiction_before_the_first_turn(tmp_path, caplog):
    from belle_agent.platforms.base import PlatformAffordances

    path = tmp_path / "config.yaml"
    path.write_text("platforms: {loopback: {enabled: true}}\n", encoding="utf-8")
    hosted = host_from_config(load_config(path), ScriptedBackend(), state_dir=tmp_path / "state")
    hosted.platform.affordances = PlatformAffordances(history="present", questions="present")
    try:
        await hosted.start()
        assert "loopback platform affordance contradiction: history: present, but recent_messages is absent" in caplog.text
        assert hosted.platform.marks == []
    finally:
        await hosted.stop()
