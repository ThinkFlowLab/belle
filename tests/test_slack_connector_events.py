"""Slack's reactions, pins, membership changes and App Home visits, through the connector.

Slack is ``tests/slack_transport.py``: no connection is opened and the tokens
are synthetic. ``tests/conformance/test_conformance_events.py`` holds what
every platform must do with an event. These checks hold what is Slack's: the
seven event types, whose action is the app's own, the kind of a conversation
an event does not state, and the words a turn is told.
"""

import json
import logging

import pytest
import pytest_asyncio
from conformance_kit import ModelTurn, worlds
from scripted_backend import ScriptedBackend
from slack_kit import SlackKit
from slack_transport import BOT_ID, BOT_USER, TEAM, FakeSlackWeb

from belle_agent.config import BelleConfigError, load_config
from belle_agent.host import OUTCOME_WITHHELD
from belle_agent.platforms.base import InboundEvent, create_platform
from belle_agent.platforms.slack import connector
from belle_agent.platforms.slack.events import render_event
from belle_agent.platforms.slack.scope_policy import slack_required_scopes
from belle_agent.platforms.slack.turn_text import (
    EVENT_TURN_DESTINATION_ROOT,
    EVENT_TURN_DESTINATION_THREAD,
    EVENT_TURN_RANKING,
    inbound_events_block,
)
from belle_agent.scopes import capabilities as scope_capabilities

ROOM = "C0ROOM0001"
OTHER_ROOM = "C0ROOM0002"
DIRECT = "D0DIRECT01"
ASKER = "U0ASKER001"
ACTOR = "U0ACTOR007"
TS = "1710000001.000100"
ROOT = "1710000000.000100"
ABOUT = {"type": "message", "channel": ROOM, "ts": TS}


def _scope(events: str, match: str = "channel: slack", delivery: str = "reply: optional") -> str:
    return f"""
scopes:
  - match: {{{match}}}
    delivery: {{mode: [all], {delivery}, events: {{{events}}}}}
"""


def _reaction(user: str = ACTOR, **fields) -> dict:
    return {"type": "reaction_added", "user": user, "reaction": "tada", "item": ABOUT, "item_user": ASKER, "event_ts": "1710000002.000100", **fields}


def _message(text: str = "hello", channel: str = ROOM, channel_type: str = "channel", ts: str = TS, **fields) -> dict:
    return {"type": "message", "channel": channel, "channel_type": channel_type, "user": ASKER, "text": text, "ts": ts} | fields


@pytest.fixture
def slack(monkeypatch) -> SlackKit:
    return SlackKit(monkeypatch)


@pytest_asyncio.fixture
async def connected(slack, tmp_path):
    """A connector with no host behind it: what it hands over is kept."""
    registry = scope_capabilities.snapshot_registry()
    path = tmp_path / "config.yaml"
    path.write_text("platforms: {slack: {enforce_audience: true}}\n", encoding="utf-8")
    platform = create_platform(load_config(path), "slack")
    received: list = []

    async def on_inbound(record) -> str:
        received.append(record)
        return "buffered:test"

    await platform.start(on_inbound)
    yield slack.sockets[-1].app, received
    await platform.stop()
    scope_capabilities.restore_registry(registry)


@pytest_asyncio.fixture
async def build_world(slack, tmp_path):
    async with worlds(ScriptedBackend(), tmp_path, slack) as build:
        yield build


# ── Slack's events as the port's record ──────────────────────────────────


@pytest.mark.parametrize(
    ("event", "kind", "conversation", "anchor"),
    [
        (_reaction(), "reaction", ROOM, TS),
        (_reaction(type="reaction_removed"), "reaction", ROOM, TS),
        ({"type": "pin_added", "user": ACTOR, "channel_id": ROOM, "item": {"type": "message", "message": {"ts": TS, "thread_ts": ROOT}}}, "pin", ROOM, TS),
        ({"type": "pin_removed", "user": ACTOR, "channel_id": ROOM, "item": {"type": "message", "message": {"ts": TS}}}, "pin", ROOM, TS),
        ({"type": "member_joined_channel", "user": ACTOR, "channel": ROOM}, "member", ROOM, ""),
        ({"type": "member_left_channel", "user": ACTOR, "channel": ROOM}, "member", ROOM, ""),
        ({"type": "app_home_opened", "user": ACTOR, "channel": DIRECT, "tab": "home"}, "app_home", DIRECT, ""),
    ],
)
async def test_each_slack_event_type_becomes_the_record_of_its_family(connected, event, kind, conversation, anchor):
    app, received = connected

    assert await app.deliver(event, event_id="Ev01") == "buffered:test"

    (record,) = received
    assert isinstance(record, InboundEvent)
    assert (record.platform, record.workspace, record.conversation, record.kind) == ("slack", TEAM, conversation, kind)
    assert (record.actor, record.event_id, record.anchor) == (ACTOR, "Ev01", anchor)
    assert record.direct is (conversation == DIRECT)
    # What a turn is told is the rendering the JiuwenSwarm channel makes of the same payload.
    assert record.record["event_type"] == event["type"] and record.record["author_user_id"] == ACTOR


async def test_a_pin_in_a_thread_is_anchored_on_that_thread_and_a_reaction_on_its_message(connected):
    app, received = connected

    await app.deliver({"type": "pin_added", "user": ACTOR, "channel_id": ROOM, "item": {"message": {"ts": TS, "thread_ts": ROOT}}})
    await app.deliver(_reaction())

    assert [(record.anchor, record.anchor_thread) for record in received] == [(TS, ROOT), (TS, TS)]


@pytest.mark.parametrize(
    ("event", "outcome"),
    [
        # The bot user of this app, as auth.test named it.
        (_reaction(user=BOT_USER), "ignored:done-by-this-bot"),
        ({"type": "pin_added", "user": BOT_USER, "channel_id": ROOM, "item": {"message": {"ts": TS}}}, "ignored:done-by-this-bot"),
        # Any app, this one or another.
        (_reaction(bot_id=BOT_ID), "ignored:done-by-an-app"),
        (_reaction(user=""), "dropped:no-actor-on-event"),
    ],
)
async def test_what_an_app_did_is_handed_to_nobody(connected, event, outcome):
    app, received = connected

    assert await app.deliver(event) == outcome
    assert received == []

    # Control: a person's reaction is handed over.
    await app.deliver(_reaction())
    assert len(received) == 1


def _users_info(app) -> list[str]:
    return [payload["user"] for method, payload in app.client.calls if method == "users_info"]


async def test_users_info_tells_another_apps_event_from_a_persons_once_for_each_actor(connected):
    app, received = connected
    app.client.bots.add("U0OTHERBOT")

    await app.deliver(_reaction(user="U0OTHERBOT"))
    await app.deliver(_reaction(user="U0OTHERBOT", type="reaction_removed"))
    await app.deliver(_reaction())
    await app.deliver({"type": "member_joined_channel", "user": ACTOR, "channel": ROOM})

    # A reaction payload carries no bot flag. The host ignores what a bot did.
    assert [(record.actor, record.actor_is_bot) for record in received] == [
        ("U0OTHERBOT", True), ("U0OTHERBOT", True), (ACTOR, False), (ACTOR, False),
    ]
    assert _users_info(app) == ["U0OTHERBOT", ACTOR]


async def test_an_event_in_a_direct_message_asks_nobody(connected):
    app, received = connected

    await app.deliver(_reaction(item={"type": "message", "channel": DIRECT, "ts": TS}))
    await app.deliver({"type": "app_home_opened", "user": ACTOR, "channel": DIRECT, "tab": "home"})

    # Only this app and one person are in it, and what this app did stopped at the gate.
    assert [record.actor_is_bot for record in received] == [False, False]
    assert _users_info(app) == []


async def test_slacks_own_users_are_bots_without_asking_in_a_direct_message_too(connected):
    app, received = connected

    await app.deliver(_reaction(user="USLACKBOT", item={"type": "message", "channel": DIRECT, "ts": TS}))
    await app.deliver(_reaction(user="USLACK"))

    # users.info would call them people.
    assert [record.actor_is_bot for record in received] == [True, True]
    assert _users_info(app) == []


async def test_an_event_whose_actor_slack_does_not_name_is_dropped(connected, caplog):
    app, received = connected
    app.client.fail = {"users_info": "user_not_found"}

    with caplog.at_level(logging.WARNING, logger=connector.logger.name):
        assert await app.deliver(_reaction()) == "dropped:actor-unknown:user_not_found"

    assert received == []
    assert any("users.info answered user_not_found" in record.getMessage() for record in caplog.records)
    # Nothing is kept of a refusal: the next event asks again.
    app.client.fail = None
    await app.deliver(_reaction())
    assert len(received) == 1 and _users_info(app) == [ACTOR, ACTOR]


async def test_without_users_read_an_event_in_a_channel_is_dropped_and_said_once(slack, tmp_path, monkeypatch, caplog):
    web = FakeSlackWeb(scopes=",".join(sorted(slack_required_scopes())))
    monkeypatch.setattr(connector, "SlackSocket", lambda *args, **kwargs: slack._socket(*args, **kwargs, web=web))
    path = tmp_path / "config.yaml"
    path.write_text("platforms: {slack: {enforce_audience: true}}\n", encoding="utf-8")
    registry = scope_capabilities.snapshot_registry()
    platform = create_platform(load_config(path), "slack")
    received: list = []

    async def on_inbound(record) -> str:
        received.append(record)
        return "buffered:test"

    await platform.start(on_inbound)
    app = slack.sockets[-1].app
    with caplog.at_level(logging.WARNING, logger=connector.logger.name):
        outcomes = [await app.deliver(_reaction()) for _ in range(2)]
        await app.deliver(_reaction(item={"type": "message", "channel": DIRECT, "ts": TS}))
    await platform.stop()
    scope_capabilities.restore_registry(registry)

    assert outcomes == ["dropped:actor-unknown:missing_scope"] * 2
    assert [record.conversation for record in received] == [DIRECT]
    assert _users_info(app) == []
    assert [record.getMessage() for record in caplog.records if "lacks users:read" in record.getMessage()] == [(
        "slack events outside direct messages are dropped: the app lacks users:read, so users.info cannot say"
        " whether a person or an app did them. Reinstall it with this scope"
    )]


async def test_the_reactions_the_connector_adds_itself_do_not_come_back_as_context(build_world):
    world = await build_world("platforms: {slack: {acknowledge_mode: reaction}}\n" + _scope("reaction: context"))
    app, web = world.platform.app, world.platform.web

    await app.deliver(_message("first"), event_id="Ev01")
    await world.host.idle()
    marks = [payload for method, payload in web.calls if method == "reactions_add"]
    assert marks, "the connector marked nothing, so this check has nothing to send back"
    # Slack tells an app of every reaction in a conversation it is in, its own among them.
    echoed = [
        await app.deliver({"type": "reaction_added", "user": BOT_USER, "reaction": mark["name"], "item": {"type": "message", "channel": ROOM, "ts": mark["timestamp"]}})
        for mark in marks
    ]
    await app.deliver(_message("second", ts="1710000003.000100"), event_id="Ev02")
    await world.host.idle()

    assert set(echoed) == {"ignored:done-by-this-bot"}
    assert "[slack_events]" not in world.requests[1].text


# ── which rule an event meets ────────────────────────────────────────────


async def test_an_event_in_a_conversation_of_unknown_kind_meets_no_rule_that_names_a_kind(build_world):
    world = await build_world(_scope("reaction: context", match="channel: slack, chat_type: [channel]"))
    app = world.platform.app

    # Slack's reaction payload states no kind, and no message has stated this conversation's.
    assert await app.deliver(_reaction()) == "ignored:events-off-for-reaction-here"

    # A message states it. It is not kept across a restart.
    await app.deliver(_message("first"), event_id="Ev01")
    await world.host.idle()
    assert await app.deliver(_reaction()) == "buffered:reaction"


async def test_a_rule_for_the_platform_or_for_one_conversation_meets_an_event_of_unknown_kind(build_world):
    world = await build_world(_scope("reaction: context", match=f"channel: slack, chat: {ROOM}"))
    app = world.platform.app

    assert await app.deliver(_reaction()) == "buffered:reaction"
    elsewhere = {**_reaction(), "item": {**ABOUT, "channel": OTHER_ROOM}}
    assert await app.deliver(elsewhere) == "ignored:events-off-for-reaction-here"


def _visit(tab: str = "messages") -> dict:
    return {"type": "app_home_opened", "user": ACTOR, "channel": DIRECT, "tab": tab}


async def _visitor_writes(world) -> str:
    """The visitor's next direct message, as its turn is told it."""
    await world.platform.app.deliver(_message("hello", channel=DIRECT, channel_type="im", user=ACTOR), event_id="EvDM")
    await world.host.idle()
    return world.requests[-1].text


async def test_an_app_home_visit_meets_no_rule_that_names_a_channel(build_world):
    world = await build_world(_scope("app_home: context", match=f"channel: slack, chat: {ROOM}"))

    # With no rule that keeps it, the visit is answered for by the welcome, which is off here.
    assert await world.platform.app.deliver(_visit()) == "ignored:home-tab-welcome-is-off"
    assert "[slack_events]" not in await _visitor_writes(world)


async def test_an_app_home_visit_cannot_be_given_a_turn(build_world):
    with pytest.raises(BelleConfigError, match=r"scopes\[0\]\.delivery\.events sets app_home=turn, and app_home cannot start a turn"):
        await build_world(_scope("app_home: turn"))


async def test_a_visit_is_welcomed_and_kept_each_by_its_own_setting(build_world):
    world = await build_world("platforms: {slack: {home_tab_welcome: true}}\n" + _scope("app_home: context"))
    app, web = world.platform.app, world.platform.web

    # One listener takes the visit. The Home tab is welcomed, and the visit is kept as the rule says.
    assert await app.deliver(_visit("home")) == "buffered:app_home"
    assert [payload["user_id"] for method, payload in web.calls if method == "views_publish"] == [ACTOR]
    assert "[slack_events]" in await _visitor_writes(world)


async def test_a_visit_no_rule_keeps_is_welcomed_all_the_same(build_world):
    world = await build_world("platforms: {slack: {home_tab_welcome: true}}\n")

    assert await world.platform.app.deliver(_visit("home")) == "welcomed:empty-home-tab"
    assert "[slack_events]" not in await _visitor_writes(world)


async def test_a_scope_that_sets_events_is_no_longer_told_that_nothing_acts_on_it(build_world, caplog):
    with caplog.at_level(logging.WARNING):
        await build_world(_scope("reaction: context, pin: turn, member: turn, app_home: context"))

    assert not [record.getMessage() for record in caplog.records if "events" in record.getMessage()]


# ── what a turn is told ──────────────────────────────────────────────────


async def test_context_is_told_in_the_words_of_the_jiuwenswarm_channel(build_world):
    world = await build_world(_scope("reaction: context"))
    app = world.platform.app

    await app.deliver(_reaction())
    await app.deliver(_message("what happened?"), event_id="Ev01")
    await world.host.idle()

    # After the message and its ids: the block the JiuwenSwarm channel writes, from the same two functions.
    said, _ids, told = world.requests[0].text.split("\n\n")
    assert said == "what happened?"
    assert told == inbound_events_block({"chat_id": ROOM, "events": [render_event(_reaction(), family="reaction")]})
    assert told.startswith('[slack_events]\n{"chat_id": "C0ROOM0001", "events": [{"family": "reaction", "event_type": "reaction_added"')
    assert '"author_user_id": "U0ACTOR007"' in told and '"emoji_name": "tada"' in told and f'"ts": "{TS}"' in told


async def test_an_event_turn_is_told_its_event_where_it_may_answer_and_that_silence_is_right(build_world):
    world = await build_world(_scope("pin: turn, member: turn", delivery="reply: optional, prompt: Greet whoever joins."))
    app = world.platform.app
    world.kit.play(ModelTurn(final="unsaid"), ModelTurn(final="unsaid"))

    pinned = await app.deliver({"type": "pin_added", "user": ACTOR, "channel_id": ROOM, "item": {"message": {"ts": TS, "thread_ts": ROOT}}}, event_id="Ev01")
    await world.host.idle()
    joined = await app.deliver({"type": "member_joined_channel", "user": ACTOR, "channel": ROOM}, event_id="Ev02")
    await world.host.idle()

    assert pinned.startswith("dispatched:event:pin") and joined.startswith("dispatched:event:member")
    pin, member = (request.text.split("\n\n") for request in world.requests)
    assert pin[0].startswith("[slack_event]\n") and json.loads(pin[0].split("\n", 1)[1])["event_type"] == "pin_added"
    # A post goes where post_message names it on this host, so the pin's turn is told to name the thread.
    assert pin[1:] == [f"[ts: {TS}, thread_ts: {ROOT}] [trigger: event]", EVENT_TURN_RANKING + connector.EVENT_REPLY_BY_TOOL]
    assert EVENT_TURN_DESTINATION_THREAD not in world.requests[0].text
    assert member[1:] == ["[trigger: event]", EVENT_TURN_RANKING + EVENT_TURN_DESTINATION_ROOT]
    for request in world.requests:
        # The standing prompt and the rule that only the posting tool speaks reach the turn as system text.
        assert request.scope.system_additions[0] == "Greet whoever joins."
        assert "post_message" in request.scope.system_additions[1]
        assert request.scope.withhold_final
    assert world.outcomes() == [OUTCOME_WITHHELD] * 2
    # The pin's turn runs in the session of the pinned message's thread, and the join's in the room's own.
    assert [request.conversation for request in world.requests] == [f"slack_{TEAM}_{ROOM}_{ROOT}", f"slack_{TEAM}_{ROOM}"]


async def test_an_event_turn_marks_no_message(build_world):
    world = await build_world("platforms: {slack: {acknowledge_mode: both}}\n" + _scope("pin: turn"))
    app, web = world.platform.app, world.platform.web

    await app.deliver({"type": "pin_added", "user": ACTOR, "channel_id": ROOM, "item": {"message": {"ts": TS}}}, event_id="Ev01")
    await world.host.idle()

    # The pinned message is somebody else's, and nobody is waiting on an acknowledgement. users.info says who pinned it.
    assert len(world.requests) == 1
    assert {method for method, _ in web.calls} <= {"auth_test", "users_info"}

    # Control: a message that starts a turn is marked.
    await app.deliver(_message("hello"), event_id="Ev02")
    await world.host.idle()
    assert any(method == "reactions_add" for method, _ in web.calls)
