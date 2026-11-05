"""What a running turn shows, where a check needs a backend to write, fail or stay undeclared on cue.

The checks that every backend passes are in ``test_conformance_progress.py``.
"""

import logging
from dataclasses import replace

import pytest
from conformance_kit import ModelTurn, RecordedBackend, World
from scripted_backend import ScriptedBackend, ScriptedTurn
from test_conformance_progress import showing, written  # noqa: F401 - `showing` is a fixture

from belle_agent import host
from belle_agent.backend import EVENT_TEXT_DELTA
from belle_agent.config import load_config
from belle_agent.host import OUTCOME_FAILED, OUTCOME_POSTED_FINAL, host_from_config
from belle_agent.platforms.base import MARK_DONE, MARK_FAILED, DeliveryUncertain


@pytest.fixture
def kit() -> ScriptedBackend:
    return ScriptedBackend()


async def test_text_written_before_a_tool_call_is_shown_and_the_reply_still_ends_as_the_final(showing):  # noqa: F811
    world = await showing()
    world.kit.play(
        ScriptedTurn(narration="Let me look.\n", final="It is noon.\n", calls=(("react_to_message", {"emoji": "eyes"}),))
    )
    message = world.direct("what time is it")

    await world.send(message)

    # What the model wrote before its tool call was shown. It is no part of what the reply ends as.
    assert written(world, message) == "Let me look.\nIt is noon.\n"
    assert world.platform.concluded == [(message.message_id, MARK_DONE, "It is noon.")]
    assert [post.text.strip() for post in world.posts()] == ["It is noon."]
    assert world.outcomes() == [OUTCOME_POSTED_FINAL]


async def test_a_reply_being_shown_when_its_turn_fails_is_ended_before_the_reader_is_told(showing):  # noqa: F811
    world = await showing()
    world.kit.play(ScriptedTurn(final="half of it\nand a half nobody gets\n", crash_mid_reply=True))
    message = world.direct("go")

    await world.send(message)

    assert written(world, message) == "half of it\nand a "
    # The showing is ended as failed, with no final. Then the one thing on screen is the failure.
    assert world.platform.concluded == [(message.message_id, MARK_FAILED, "")]
    assert [post.text for post in world.posts()] == ["The turn failed: RuntimeError"]
    assert world.outcomes() == [OUTCOME_FAILED]


async def test_uncertain_stream_delivery_does_not_post_the_final_again(showing, monkeypatch, caplog):  # noqa: F811
    world = await showing()

    async def uncertain(*_args):
        raise DeliveryUncertain("the streamed message may exist")

    async def no_preview(*_args):
        pass

    monkeypatch.setattr(world.host.platform, "progress", no_preview)
    monkeypatch.setattr(world.host.platform, "conclude", uncertain)
    world.kit.play(ScriptedTurn(final="the answer\n"))

    await world.send(world.direct("go"))

    assert world.posts() == []
    assert world.outcomes() == [OUTCOME_FAILED]
    assert any("automatic final posting refused" in message for message in caplog.messages)


async def test_a_backend_that_declares_no_progress_has_nothing_shown_and_the_host_says_so(tmp_path, caplog):
    undeclared = ScriptedBackend(replace(ScriptedBackend().capabilities(), reports=frozenset()))
    path = tmp_path / "config.yaml"
    path.write_text("platforms: {loopback: {enabled: true}}\n", encoding="utf-8")
    quiet = host_from_config(load_config(path), undeclared, state_dir=tmp_path / "state")
    world = World(quiet, quiet.platform, undeclared, RecordedBackend(undeclared))

    with caplog.at_level(logging.WARNING, logger=host.__name__):
        await quiet.start()
    try:
        undeclared.play(ModelTurn(final="an answer", calls=(("react_to_message", {"emoji": "eyes"}),)))
        await world.send(world.direct("hello"))
    finally:
        await quiet.stop()

    # The backend yields the events all the same. It did not declare them, so none is handed over.
    assert quiet.platform.progressed == [] and quiet.platform.concluded == []
    assert [post.text for post in world.posts()] == ["an answer"]
    told = [record.getMessage() for record in caplog.records if "shows nothing" in record.getMessage()]
    assert len(told) == 1 and "backend scripted reports no" in told[0] and EVENT_TEXT_DELTA in told[0]


def test_a_backend_cannot_declare_progress_the_port_does_not_define():
    with pytest.raises(ValueError, match="unknown progress events: succeeded"):
        replace(ScriptedBackend().capabilities(), reports=frozenset({EVENT_TEXT_DELTA, "succeeded"}))
