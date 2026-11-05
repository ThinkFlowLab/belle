"""Events and the decision gate beside the other Slack surfaces of the connector.

Several workspaces, what a running turn shows, the command surface and held
calls each have checks of their own. These hold what an event and a decision
do where they meet one of them.
"""

import asyncio
import json
import logging
from pathlib import Path

import pytest
import pytest_asyncio
import yaml
from conformance_kit import ModelTurn, worlds
from decision_standin import KEY, KEY_ENV, DecisionStandIn, logged
from scripted_backend import ScriptedBackend, ScriptedTurn
from slack_transport import BOT_USER, SECOND_TEAM, TEAM, dm_of
from test_slack_connector_commands import _say, _slash
from test_slack_turn_surfaces import _cards, _methods, clock  # noqa: F401
from test_slack_workspaces import TWO, _sent, build, slack, two  # noqa: F401

from belle_agent.host import OUTCOME_INTERRUPTED, OUTCOME_WITHHELD
from belle_agent.message_decisions import ADVISORY_PREAMBLE
from belle_agent.platforms.base import InboundDrop, InboundStop

ROOM = "C0ROOM0001"
OTHER_ROOM = "C0ROOM0002"
ASKER = "U0ASKER001"
BYSTANDER = "U0OTHER002"
ACTOR = "U0ACTOR007"
TS = "1710000001.000100"
LATER = "1710000002.000100"

SHOWING = "platforms: {slack: {enforce_audience: true, activity_card: true, enable_streaming: true}}\n"


def _scope(events: str, delivery: str = "reply: optional") -> str:
    return f"scopes:\n  - match: {{channel: slack}}\n    delivery: {{mode: [all], {delivery}, events: {{{events}}}}}\n"


def _reaction(user: str = ACTOR) -> dict:
    return {"type": "reaction_added", "user": user, "reaction": "tada", "item": {"type": "message", "channel": ROOM, "ts": TS}}


def _joined(user: str = ACTOR) -> dict:
    return {"type": "member_joined_channel", "user": user, "channel": ROOM}


def _pinned() -> dict:
    return {"type": "pin_added", "user": ACTOR, "channel_id": ROOM, "item": {"message": {"ts": TS}}}


def _message(text: str, ts: str = TS, **fields) -> dict:
    return {"type": "message", "channel": ROOM, "channel_type": "channel", "user": ASKER, "text": text, "ts": ts} | fields


@pytest_asyncio.fixture
async def build_world(slack, tmp_path):  # noqa: F811
    async with worlds(ScriptedBackend(), tmp_path, slack) as build_one:
        yield build_one


@pytest.fixture
def decisions(monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)
    endpoint = DecisionStandIn()
    yield endpoint
    endpoint.close()


async def _asked(endpoint: DecisionStandIn, count: int) -> None:
    """Wait until the endpoint has been asked ``count`` times. A logged decision waits for nobody."""
    async with asyncio.timeout(10):
        while len(endpoint.log) < count:
            await asyncio.sleep(0.01)


# ── several workspaces ───────────────────────────────────────────────────


async def test_an_event_of_one_workspace_reaches_no_client_and_no_turn_of_another(two):  # noqa: F811
    events = "  - match: {channel: slack}\n    delivery: {reply: optional, events: {reaction: context, member: turn}}\n"
    world, first, second = await two(TWO.replace("acknowledge_mode: reaction", "acknowledge_mode: 'off'") + events)
    world.kit.play(ModelTurn(calls=(("post_message", {"text": "welcome"}),)))

    # The same channel id in both workspaces. The reaction and the join happen in the second.
    assert await second.app.deliver(_reaction()) == "buffered:reaction"
    assert (await second.app.deliver(_joined(), event_id="EvJoin")).startswith("dispatched:event:member")
    await world.host.idle()

    # The turn of the join ran in the second workspace, and spoke through its client.
    assert world.requests[0].metadata["slack_team_id"] == SECOND_TEAM
    assert (_sent(first), _sent(second)) == ([], [(ROOM, "welcome")])
    assert {method for method, _ in first.web.calls} == {"auth_test"}
    # It took the reaction kept there. A turn of the first workspace, in the channel of the same id, is told nothing.
    assert "[slack_events]" in world.requests[0].text
    await second.app.deliver(_reaction())
    await first.app.deliver(_message("what happened here?"), event_id="Ev1")
    await world.host.idle()
    assert "[slack_events]" not in world.requests[1].text
    # Control: the next turn of the second workspace is told it.
    await second.app.deliver(_message("and here?"), event_id="Ev2")
    await world.host.idle()
    assert "[slack_events]" in world.requests[2].text


async def test_the_gate_reads_the_messages_before_a_message_in_its_own_workspace_only(two, decisions):  # noqa: F811
    world, first, second = await two(decisions.config().replace("loopback", "slack") + TWO)
    second.web.history[ROOM] = [{"user": BYSTANDER, "text": "said in the second workspace", "ts": "1710000000.000100"}]
    first.web.history[ROOM] = [{"user": BYSTANDER, "text": "said in the first workspace", "ts": "1710000000.000100"}]

    await second.app.deliver(_message("hello"), event_id="Ev1")
    await world.host.idle()
    await _asked(decisions, 1)

    (state,) = decisions.states()
    assert [entry["text"] for entry in state["recent_messages"]] == ["said in the second workspace"]
    assert first.web.payloads("conversations_history") == [] and len(second.web.payloads("conversations_history")) == 1


# ── what a running turn shows ────────────────────────────────────────────


async def test_the_turn_of_an_event_shows_nothing_with_the_reply_and_the_card_switched_on(build_world, clock):  # noqa: F811
    world = await build_world(SHOWING + _scope("pin: turn"))
    web = world.platform.web
    working = ScriptedTurn(narration="Let me look.\n", final="unsaid\n", calls=(("react_to_message", {"emoji": "eyes"}),), hold_before_final=True)
    world.kit.play(working, working)

    assert (await world.platform.app.deliver(_pinned(), event_id="EvPin")).startswith("dispatched:event:pin")
    await world.kit.holding()
    await clock.advance(60)
    world.kit.release()
    await world.host.idle()

    # No mark, no status, no card and no reply as it is written: the one write is the reaction the turn made itself.
    assert world.outcomes() == [OUTCOME_WITHHELD]
    reads = {"auth_test", "users_info"}
    assert [(method, payload.get("name")) for method, payload in web.calls if method not in reads] == [("reactions_add", "eyes")]
    assert _cards(web) == [] and _methods(web) == [] and world.platform.progressed == []

    # Control: the same work for a message of the same room is acknowledged and shown on a card.
    await world.platform.app.deliver(_message("look", ts=LATER), event_id="Ev1")
    await world.kit.holding()
    await clock.advance(60)
    assert len(_cards(web)) == 1 and world.platform.progressed
    world.kit.release()
    await world.host.idle()


# ── the command surface ──────────────────────────────────────────────────


async def test_status_counts_the_turn_of_an_event_the_events_that_wait_and_those_lost(build_world):
    world = await build_world(_scope("member: turn, pin: turn", delivery="reply: optional, session: channel"))
    app = world.platform.app
    world.kit.play(ModelTurn(final="about the join", hold=True))

    await app.deliver(_joined(), event_id="EvJoin1")
    await world.kit.holding()
    assert (await app.deliver(_joined(BYSTANDER), event_id="EvJoin2")).startswith("held:member")
    # A pin that names no message gets no turn.
    assert await app.deliver({"type": "pin_added", "user": ACTOR, "channel_id": ROOM, "item": {}}, event_id="EvPin") == "dropped:pin-no-anchor"

    status = (await _slash(world, "status")).texts[-1]
    assert "1 turn running, 1 event turn waiting. A message sent during an event turn waits until it ends" in status
    assert "*Event turns lost since restart*: 1." in status
    # An event that waits is no message: the queue does not list it, and nobody can remove it.
    assert (await _slash(world, "queue list")).texts == ["No messages are waiting in this channel or direct message."]
    assert world.host.waiting(TEAM, ROOM) == []

    # The turn of an event is a running turn: it is stopped by the rule every stop is held to.
    (running,) = world.host.running(TEAM, ROOM)
    assert world.host.may_stop(running, ASKER) == (True, "allow_from")
    stop = InboundStop(platform="slack", workspace=TEAM, conversation=ROOM, message_id=running.inbound.message_id, sender=ASKER)
    assert (await world.host.receive(stop)).startswith("stopped")
    world.kit.release()
    await world.host.idle()

    # The event that waited then got its turn.
    assert world.outcomes() == [OUTCOME_INTERRUPTED, OUTCOME_WITHHELD]
    assert "nothing running" in (await _slash(world, "status")).texts[-1]


# ── held calls ───────────────────────────────────────────────────────────


async def test_a_post_of_an_event_turn_that_would_reach_new_readers_is_refused_and_nobody_is_asked(build_world):
    world = await build_world(_scope("member: turn"))
    web = world.platform.web
    web.members.update({ROOM: [ASKER, ACTOR, BOT_USER], OTHER_ROOM: [ASKER, BYSTANDER, BOT_USER]})
    elsewhere = (("post_message", {"text": "for other eyes", "chat_id": OTHER_ROOM}),)
    world.kit.play(ModelTurn(calls=elsewhere), ModelTurn(calls=elsewhere))

    await world.platform.app.deliver(_joined(), event_id="EvJoin")
    await world.host.idle()

    # Nobody asked for this turn, so there is nobody to put the question to: the call is refused with the reason.
    refused = json.loads(world.kit.results[0])
    assert refused["ok"] is False and BYSTANDER in json.dumps(refused)
    assert web.payloads("chat_postMessage") == [] and world.host.held(TEAM, ROOM) == 0

    # Control: the same call from the turn of a person's message is put to the room, and held.
    await world.platform.app.deliver(_message("post it over there"), event_id="Ev1")
    await world.host.idle()
    (question,) = web.payloads("chat_postMessage")
    assert question["channel"] == ROOM and question.get("blocks") and world.host.held(TEAM, ROOM) == 1
    assert world.outcomes() == [OUTCOME_WITHHELD, OUTCOME_WITHHELD]


# ── decisions ────────────────────────────────────────────────────────────


async def test_a_question_asked_privately_is_put_to_the_gate_as_the_direct_message_it_is(build_world, decisions, caplog):
    world = await build_world(decisions.config(forward=True).replace("loopback", "slack"))

    with caplog.at_level(logging.INFO, logger="belle_agent.message_decisions"):
        await _slash(world, "ask what is the plan for Friday?")
        await world.host.idle()
        (record,) = await logged(caplog)

    # As the JiuwenSwarm channel does: the anchor posted in the person's direct message is admitted as their message.
    (state,) = decisions.states()
    assert state["channel"] == dm_of(ASKER) and state["conversation_kind"] == "one_to_one_dm"
    assert "what is the plan for Friday?" in state["last_message"]["text"]
    assert (record["channel"], record["user"], record["is_dm"]) == (dm_of(ASKER), ASKER, True)
    assert ADVISORY_PREAMBLE in world.requests[0].text


async def test_a_waiting_message_that_is_removed_leaves_nothing_of_its_decision_behind(build_world, decisions):
    world = await build_world(decisions.config(forward=True).replace("loopback", "slack") + _scope("reaction: 'off'", delivery="reply: required"))
    world.kit.play(ModelTurn(final="first", hold=True))

    await _say(world, "one", ts=TS)
    await world.kit.holding()
    assert (await _say(world, "two", ts=LATER, thread=TS)).startswith("queued")
    drop = InboundDrop(platform="slack", workspace=TEAM, conversation=ROOM, message_id=LATER, sender=ASKER)
    assert (await world.host.receive(drop)).startswith("removed:")
    world.kit.release()
    await world.host.idle()
    await _asked(decisions, 2)

    # It was put to the gate once, on arrival. Nothing is kept for a turn it will not have.
    assert len(decisions.log) == 2 and world.host.platform._decision_inputs == {}
    assert [ADVISORY_PREAMBLE in request.text for request in world.requests] == [True]
    assert len(world.outcomes()) == 1


# ── the example configuration ────────────────────────────────────────────


async def test_the_scope_defaults_the_example_offers_are_acted_on_here(build_world, caplog):
    example = yaml.safe_load((Path(__file__).resolve().parents[1] / "examples/config/belle-config.yaml").read_text(encoding="utf-8"))

    with caplog.at_level(logging.WARNING):
        world = await build_world(yaml.safe_dump({"scopes": example["scopes"]}))
    await world.platform.app.deliver(_message("just talking"), event_id="Ev1")
    await world.host.idle()

    # No rule is refused by the scope loader, and none is named as doing nothing on a backend that applies scopes.
    assert not [record.getMessage() for record in caplog.records if "scopes" in record.getMessage()]
    scope = world.requests[0].scope
    assert scope.skills_required == ("messaging-context", "group-chat-participation", "session-work") and scope.withhold_final
    # The skills the example makes unavailable on Mattermost stay usable on Slack.
    assert scope.skills_unavailable == frozenset()
    assert world.requests[0].conversation == f"slack_{TEAM}_{ROOM}"
