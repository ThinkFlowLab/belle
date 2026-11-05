"""What becomes of an event that is no message: nothing, context for the next turn, or a turn of its own.

An event is told to the turns of the conversation it happened in and to no
other. One that is refused, lost or expired is told to nobody.
"""

import itertools
import logging

import pytest
from conformance_kit import WORKSPACE, ModelTurn, World

from belle_agent import host
from belle_agent.host import events as host_events
from belle_agent.platforms.base import InboundEvent

_ids = itertools.count(1)

#: Ids no message of these checks carries, so a turn's text shows whether it was told the event.
ACTOR = "U7ACTOR"
OTHER_ACTOR = "U8ACTOR"


def scope(events: str, *, delivery: str = "reply: optional") -> str:
    return f"""
scopes:
  - match: {{channel: loopback}}
    delivery: {{mode: [all], {delivery}, events: {{{events}}}}}
"""


def event(world: World, kind: str, *, conversation: str = "C1", actor: str = ACTOR, **fields) -> InboundEvent:
    """Build one event with a fresh delivery id. The record names who did it, as every platform's does."""
    return InboundEvent(
        platform=world.platform_kit.name,
        workspace=WORKSPACE,
        conversation=conversation,
        kind=kind,
        actor=actor,
        record={"kind": kind, "actor": actor},
        event_id=f"ev{next(_ids)}",
        direct=conversation.startswith("D"),
        **fields,
    )


def told(world: World, actor: str = ACTOR) -> list[bool]:
    """Whether each turn the backend was handed names the one who did the event."""
    return [actor in request.text for request in world.requests]


# ── context ──────────────────────────────────────────────────────────────


async def test_an_event_kept_as_context_reaches_the_next_turn_of_its_conversation_and_no_other(build_world):
    world = await build_world(scope("reaction: context"))

    assert await world.send(event(world, "reaction", anchor="m-about")) == "buffered:reaction"
    assert world.requests == []

    # Somebody else's direct message and another room are other audiences.
    await world.send(world.direct("a private question", sender="U2"))
    await world.send(world.message("elsewhere", conversation="C2"))
    # The room it happened in, whoever speaks next and in whichever thread.
    await world.send(world.message("here", sender="U3", thread="some-thread"))
    # It is told once.
    await world.send(world.message("here again"))

    assert told(world) == [False, False, True, False]


async def test_an_event_is_dropped_where_no_rule_keeps_its_kind(build_world):
    world = await build_world(scope("reaction: context"))

    assert await world.send(event(world, "member")) == "ignored:events-off-for-member-here"
    await world.send(world.message("here"))

    assert told(world) == [False]


async def test_what_the_bot_did_itself_is_no_event(build_world):
    world = await build_world(scope("reaction: context"))

    outcome = await world.send(event(world, "reaction", anchor="m-about", actor_is_bot=True))
    await world.send(world.message("here"))

    assert outcome.startswith("ignored:done-by-")
    assert told(world) == [False]
    assert '"events"' not in world.requests[0].text

    # Control: the same reaction by a person is told to the next turn.
    await world.send(event(world, "reaction", anchor="m-about"))
    await world.send(world.message("here again"))
    assert told(world) == [False, True]


async def test_an_event_by_someone_who_may_not_ask_is_told_to_nobody(build_world):
    world = await build_world("platforms: {loopback: {allow_from: [U1, U7ACTOR]}}\n" + scope("reaction: context"))

    refused = await world.send(event(world, "reaction", anchor="m-about", actor=OTHER_ACTOR))
    kept = await world.send(event(world, "reaction", anchor="m-about"))
    await world.send(world.message("here"))

    assert (refused, kept) == ("refused:actor-not-in-allow_from", "buffered:reaction")
    assert told(world, OTHER_ACTOR) == [False]
    assert told(world) == [True]


async def test_a_visit_is_told_to_the_visitor_s_own_direct_turn(build_world):
    world = await build_world(scope("app_home: context"))

    assert await world.send(event(world, "app_home", conversation=f"D-{ACTOR}")) == "buffered:app_home"
    await world.send(world.direct("somebody else", sender="U2"))
    await world.send(world.direct("the visitor", sender=ACTOR))

    assert ['"events"' in request.text for request in world.requests] == [False, True]


async def test_a_conversation_keeps_a_bounded_number_of_events_and_says_how_many_it_dropped(build_world):
    world = await build_world(scope("app_home: context"))
    held = world.host.platform.event_kinds["app_home"].held

    for _ in range(held + 2):
        await world.send(event(world, "app_home", conversation=f"D-{ACTOR}"))
    await world.send(world.direct("the visitor", sender=ACTOR))

    assert '"overflow": 2' in world.requests[0].text


# ── turns ────────────────────────────────────────────────────────────────


async def test_an_event_starts_a_turn_that_may_stay_silent(build_world):
    world = await build_world(scope("member: turn"))
    world.kit.play(ModelTurn(final="nothing worth saying"))

    outcome = await world.send(event(world, "member"))

    assert outcome.startswith("dispatched:event:member")
    assert told(world) == [True]
    assert world.requests[0].scope.withhold_final
    # Tool-only silence: the turn ended and posted nothing.
    assert world.posts() == []
    assert world.outcomes() == [host.OUTCOME_WITHHELD]


async def test_an_event_turn_speaks_where_its_event_happened(build_world):
    world = await build_world(scope("member: turn, pin: turn"))
    pinned = "1710000001.000100"
    world.kit.play(
        ModelTurn(calls=(("post_message", {"text": "welcome"}),)),
        ModelTurn(calls=(("post_message", {"text": "noted", "reply_to_message_id": pinned}),)),
    )

    await world.send(event(world, "member"))
    await world.send(event(world, "pin", anchor=pinned, anchor_thread=pinned))

    # The turn of a pin is told which message was pinned, so it can answer in that message's thread.
    assert pinned in world.requests[1].text
    # A change of members is answered at the top level of the conversation it happened in.
    assert [(post.conversation, post.thread, post.text) for post in world.posts()] == [
        ("C1", "", "welcome"),
        ("C1", pinned, "noted"),
    ]
    assert world.outcomes() == [host.OUTCOME_POSTED_BY_TOOL] * 2


async def test_an_event_turn_where_a_reply_is_owed_is_kept_as_context(build_world):
    world = await build_world(scope("member: turn", delivery="reply: required"))

    outcome = await world.send(event(world, "member"))
    assert outcome == "buffered:member (turn-downgraded:reply-required)"
    assert world.requests == []

    await world.send(world.message("here"))
    assert told(world) == [True]


async def test_a_second_delivery_of_one_event_starts_no_second_turn(build_world):
    world = await build_world(scope("member: turn"))
    joined = event(world, "member")

    await world.send(joined)
    assert (await world.send(joined)).startswith("ignored:already-handled (dedupe)")
    assert len(world.requests) == 1

    # Control: another join is another turn.
    await world.send(event(world, "member"))
    assert len(world.requests) == 2


async def test_a_member_event_posts_in_its_resolved_conversation(build_world):
    world = await build_world(scope("member: turn"))
    world.kit.play(ModelTurn(calls=(("post_message", {"text": "welcome"}),)))

    await world.send(event(world, "member"))

    assert [(post.conversation, post.thread, post.text) for post in world.posts()] == [("C1", "", "welcome")]
    assert world.outcomes() == [host.OUTCOME_POSTED_BY_TOOL]


async def test_an_event_that_finds_its_conversation_busy_waits_behind_the_messages(build_world):
    world = await build_world(scope("member: turn", delivery="reply: optional, session: channel"))
    world.kit.play(ModelTurn(final="one", hold=True))

    await world.platform.inject(world.message("first"))
    await world.kit.holding()
    held = await world.platform.inject(event(world, "member"))
    queued = await world.platform.inject(world.message("second"))
    world.kit.release()
    await world.host.idle()

    assert held.startswith("held:member")
    assert queued.startswith("queued:")
    # The message that waited runs before the event that waited, although it arrived after it.
    assert told(world) == [False, False, True]
    assert "second" in world.requests[1].text


async def test_a_message_waits_for_the_turn_of_an_event_whatever_mid_turn_says(build_world):
    world = await build_world(scope("member: turn", delivery="reply: optional, session: channel, mid_turn: cancel"))
    world.kit.play(ModelTurn(final="about the join", hold=True))

    await world.platform.inject(event(world, "member"))
    await world.kit.holding()
    outcome = await world.platform.inject(world.message("a message"))
    world.kit.release()
    await world.host.idle()

    assert outcome.startswith("queued:")
    # The event's turn was not stopped for the message.
    assert world.outcomes() == [host.OUTCOME_WITHHELD] * 2


async def test_a_reaction_that_finds_its_thread_busy_is_kept_as_context(build_world):
    world = await build_world(scope("reaction: turn"))
    world.kit.play(ModelTurn(final="one", hold=True))

    await world.platform.inject(world.message("first", thread="root"))
    await world.kit.holding()
    outcome = await world.platform.inject(event(world, "reaction", anchor="m-about", anchor_thread="root"))
    world.kit.release()
    await world.host.idle()
    await world.send(world.message("second", thread="root"))

    assert outcome == "buffered:reaction (turn-downgraded:turn-in-flight)"
    assert told(world) == [False, True]


# ── bounds and losses ────────────────────────────────────────────────────


async def test_an_event_that_cannot_wait_is_lost_and_told_to_nobody(build_world, monkeypatch, caplog):
    monkeypatch.setattr(host_events, "MAX_PENDING_EVENTS_PER_KEY", 1)
    world = await build_world(scope("member: turn", delivery="reply: optional, session: channel"))
    world.kit.play(ModelTurn(final="one", hold=True))

    await world.platform.inject(world.message("first"))
    await world.kit.holding()
    with caplog.at_level(logging.WARNING, logger=host_events.__name__):
        waiting = await world.platform.inject(event(world, "member"))
        lost = event(world, "member", actor=OTHER_ACTOR)
        outcome = await world.platform.inject(lost)
    world.kit.release()
    await world.host.idle()
    await world.send(world.message("later"))

    assert waiting.startswith("held:member")
    assert outcome == "dropped:pending-member-full"
    # The loss is said with what recovers it by hand: the event, who did it, where, and why.
    line = next(record.getMessage() for record in caplog.records if "event turn lost" in record.getMessage())
    assert all(part in line for part in (lost.event_id, OTHER_ACTOR, "conversation=C1", "reason=admission-full"))
    assert world.host.lost_events == {(WORKSPACE, "C1"): 1}
    assert world.host.events_of(WORKSPACE, "C1") == (False, 0, 1)
    # It got no turn, and no later turn is told it.
    assert told(world, OTHER_ACTOR) == [False, False, False]
    assert told(world) == [False, True, False]


async def test_an_event_that_waited_too_long_is_lost_and_told_to_nobody(build_world, monkeypatch, caplog):
    world = await build_world(scope("member: turn", delivery="reply: optional, session: channel"))
    world.kit.play(ModelTurn(final="one", hold=True))

    await world.platform.inject(world.message("first"))
    await world.kit.holding()
    assert (await world.platform.inject(event(world, "member"))).startswith("held:member")
    monkeypatch.setattr(host_events, "PENDING_EVENT_TIMEOUT_SECONDS", 0.0)
    with caplog.at_level(logging.WARNING, logger=host_events.__name__):
        world.kit.release()
        await world.host.idle()
    await world.send(world.message("later"))

    assert any("reason=expired" in record.getMessage() for record in caplog.records)
    assert told(world) == [False, False]


async def test_a_pin_that_names_no_message_is_lost_and_not_kept(build_world, caplog):
    world = await build_world(scope("pin: turn"))

    with caplog.at_level(logging.WARNING, logger=host_events.__name__):
        outcome = await world.send(event(world, "pin"))
    await world.send(world.message("later"))

    assert outcome == "dropped:pin-no-anchor"
    assert any("reason=no-anchor" in record.getMessage() for record in caplog.records)
    assert told(world) == [False]


@pytest.mark.parametrize("kind", ["member", "pin"])
async def test_events_lost_when_the_host_stops_are_each_named(build_world, caplog, kind):
    world = await build_world(scope(f"{kind}: turn", delivery="reply: optional, session: channel"))
    world.kit.play(ModelTurn(final="one", hold=True))

    await world.platform.inject(world.message("first"))
    await world.kit.holding()
    waiting = event(world, kind, anchor="m-about")
    await world.platform.inject(waiting)
    with caplog.at_level(logging.WARNING, logger=host_events.__name__):
        await world.host.stop()

    assert any(
        waiting.event_id in record.getMessage() and "reason=host-stopped" in record.getMessage()
        for record in caplog.records
    )
