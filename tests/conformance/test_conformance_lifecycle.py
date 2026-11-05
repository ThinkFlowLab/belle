"""Stopping a turn, a request that comes twice, and a turn that does not end. Every backend must pass."""

import asyncio
from dataclasses import replace

from conformance_kit import ModelTurn

from belle_agent import host
from belle_agent.host import OUTCOME_DUPLICATE, OUTCOME_FAILED, OUTCOME_INTERRUPTED, OUTCOME_POSTED_BY_TOOL

STOP_IS_FOR_THE_LEAD = """
scopes:
  - match: {channel: loopback, chat: C1}
    delivery: {mode: [all]}
    clicks: {stop: {user: [U-LEAD]}}
"""


def _reactions(world) -> list:
    return [action for action in world.platform.log if action.kind == "reaction"]


async def test_a_persons_stop_ends_the_turn_before_what_it_was_about_to_do(world):
    marker = ModelTurn(final="said after the stop", hold=True, calls=(("post_message", {"text": "the marker"}),))
    world.kit.play(marker, ModelTurn(final="done", calls=(("post_message", {"text": "the control"}),)))
    message = world.direct("do it")
    await world.platform.inject(message)
    await world.kit.holding()

    assert (await world.platform.inject(world.stop(message))).startswith("stopped")
    world.kit.release()
    await world.host.idle()

    # A stop is an absent side effect: neither the post the turn was about to make nor its final.
    assert world.platform.log == []
    assert world.outcomes() == [OUTCOME_INTERRUPTED]
    # The turn is over, so there is nothing left to stop.
    assert await world.platform.inject(world.stop(message)) == "ignored:no-turn-running"

    # Control: the conversation takes its next turn, and the same call posts.
    await world.send(world.direct("do it now"))
    assert [post.text for post in world.posts()] == ["the control"]
    assert world.outcomes() == [OUTCOME_INTERRUPTED, OUTCOME_POSTED_BY_TOOL]
    assert world.requests[0].conversation == world.requests[1].conversation


async def test_a_clicks_rule_decides_who_else_may_stop_a_turn(build_world):
    world = await build_world(STOP_IS_FOR_THE_LEAD)
    world.kit.play(ModelTurn(final="never said", hold=True), ModelTurn(final="never said", hold=True))
    first = world.message("one", sender="U1", thread="a")
    await world.platform.inject(first)
    await world.kit.holding()

    # Seeing a turn does not grant the right to stop it.
    assert (await world.platform.inject(world.stop(first, sender="U2"))).startswith("refused:clicks.stop")
    assert world.outcomes() == []
    # The person the rule names may, and so may whoever started the turn, whatever the rule says.
    assert (await world.platform.inject(world.stop(first, sender="U-LEAD"))).startswith("stopped")
    world.kit.release()
    await world.host.idle()

    second = world.message("two", sender="U1", thread="b")
    await world.platform.inject(second)
    await world.kit.holding()
    assert (await world.platform.inject(world.stop(second))).startswith("stopped")
    world.kit.release()
    await world.host.idle()

    assert world.outcomes() == [OUTCOME_INTERRUPTED, OUTCOME_INTERRUPTED]
    assert world.posts() == []


async def test_a_request_that_comes_twice_runs_once_and_is_answered_once(world):
    world.kit.play(ModelTurn(final="the answer"), ModelTurn(final="another answer"))
    first = world.direct("a question")
    await world.send(first)

    # The same request under a message id the host has not seen: its event id window does not hold it back.
    async with asyncio.timeout(10):
        await world.send(replace(first, message_id="seen-as-new"))

    assert [request.request_id for request in world.requests] == [first.event_id, first.event_id]
    assert [post.text for post in world.posts()] == ["the answer"]
    assert world.outcomes() == [host.OUTCOME_POSTED_FINAL, OUTCOME_DUPLICATE]

    # Control: the same text under a new request id is a new turn.
    await world.send(world.direct("a question"))
    assert [post.text for post in world.posts()] == ["the answer", "another answer"]


async def test_a_turn_that_asks_the_model_without_end_is_stopped(world, monkeypatch):
    monkeypatch.setattr(host, "MAX_TURN_STEPS", 3)
    runaway = tuple(("react_to_message", {"emoji": f"loop-{number}"}) for number in range(40))
    world.kit.play(ModelTurn(final="never said", calls=runaway), ModelTurn(final="fine now"))

    async with asyncio.timeout(30):
        await world.send(world.direct("go"))

    # The reader is owed an answer, so it is told. The loop did not run to its end.
    assert [post.text for post in world.posts()] == [host.TURN_STEPS_NOTICE]
    assert world.outcomes() == [OUTCOME_FAILED]
    assert len(_reactions(world)) < len(runaway)

    # Control: a turn inside the bound runs, in the same conversation.
    stopped_at = len(_reactions(world))
    await world.send(world.direct("again"))
    assert [post.text for post in world.posts()] == [host.TURN_STEPS_NOTICE, "fine now"]
    # Nothing of the stopped turn arrived late.
    assert len(_reactions(world)) == stopped_at
