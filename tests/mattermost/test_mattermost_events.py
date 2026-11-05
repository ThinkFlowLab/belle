"""Reactions and members who join or leave a channel, handed to the host as events."""

import json

import pytest
from transport import BOT_USER, TOKEN, URL, EventFeeder, FakeRest, hello

from belle_agent.host.events import EVENT_TURN_DESTINATION_ROOT, EVENT_TURN_RANKING
from belle_agent.platforms.base import InboundEvent
from belle_agent.platforms.mattermost.client import MattermostClient
from belle_agent.platforms.mattermost.connector import MattermostPlatform
from belle_agent.platforms.mattermost.events import REPLY_BY_TOOL
from belle_agent.platforms.mattermost.settings import MattermostSettings

CHANNEL = "c" * 26
TEAM = "t" * 26
ALICE = "a" * 26
POST, ROOT = "p" * 26, "r" * 26


@pytest.fixture
async def connected():
    rest = FakeRest()
    rest.channels[CHANNEL] = {"id": CHANNEL, "type": "O", "team_id": TEAM}
    rest.post(POST, CHANNEL, "b" * 26, "the plan", root=ROOT)
    client = MattermostClient(URL, TOKEN, transport=rest.transport(), open_events=EventFeeder(hello()).open)
    connector = MattermostPlatform(MattermostSettings(URL, TOKEN), client=client)
    records = []

    async def host(record):
        records.append(record)
        return "buffered:test"

    await connector.start(host)
    yield connector, rest, records
    await connector.stop()


def _reaction(kind: str = "reaction_added", user: str = ALICE, channel: str = CHANNEL, seq: int = 5) -> dict:
    # As the server sends them: an added reaction carries its time, a removed one none.
    created = 1_800_000_000_123 if kind == "reaction_added" else 0
    reaction = {"user_id": user, "post_id": POST, "emoji_name": "tada", "channel_id": channel, "create_at": created}
    return {"event": kind, "data": {"reaction": json.dumps(reaction)}, "broadcast": {"channel_id": CHANNEL}, "seq": seq}


async def test_a_reaction_is_an_event_about_its_message_in_that_message_s_thread(connected):
    connector, _, records = connected

    assert await connector.handle(_reaction()) == "buffered:test"

    (event,) = records
    assert isinstance(event, InboundEvent)
    assert (event.kind, event.actor, event.conversation, event.workspace, event.chat_type) == (
        "reaction", ALICE, CHANNEL, TEAM, "O"
    )
    assert (event.anchor, event.anchor_thread, event.direct) == (POST, ROOT, False)
    assert event.record == {"family": "reaction", "event_type": "reaction_added", "emoji_name": "tada",
                            "message_id": POST, "author_user_id": ALICE, "chat_id": CHANNEL}


async def test_an_added_reaction_keeps_its_event_id_whichever_connection_carries_it(connected):
    connector, _, records = connected

    # A read-back after a gap hands over the reaction again, with no sequence number.
    await connector.handle(_reaction(seq=5))
    await connector.handle(_reaction(seq=17))

    first, again = records
    assert first.event_id == again.event_id
    assert not first.event_id.endswith(":5")


async def test_a_removed_reaction_takes_its_channel_from_the_broadcast(connected):
    connector, _, records = connected

    await connector.handle(_reaction("reaction_removed", channel=""))

    assert records[0].conversation == CHANNEL and records[0].record["event_type"] == "reaction_removed"
    # A removal carries no time to name it by, and is never read back: the connection's number names it.
    assert records[0].event_id.endswith(":5")


async def test_the_bot_s_own_reaction_is_no_event(connected):
    connector, _, records = connected

    assert await connector.handle(_reaction(user=BOT_USER)) == "ignored:done-by-this-bot"
    assert records == []


async def test_a_member_who_joins_or_leaves_is_an_event_of_the_channel(connected):
    connector, _, records = connected

    for kind in ("user_added", "user_removed"):
        await connector.handle({"event": kind, "data": {"user_id": ALICE, "team_id": TEAM},
                                "broadcast": {"channel_id": CHANNEL}, "seq": 9})

    assert [(event.kind, event.actor, event.anchor, event.record["event_type"]) for event in records] == [
        ("member", ALICE, "", "user_added"), ("member", ALICE, "", "user_removed")
    ]


def test_an_event_s_turn_is_told_the_event_where_it_happened_and_where_to_answer(connected):
    connector, _, _ = connected
    reaction = InboundEvent("mattermost", TEAM, CHANNEL, "reaction", ALICE, {"emoji_name": "tada"}, anchor=POST,
                            anchor_thread=ROOT)
    member = InboundEvent("mattermost", TEAM, CHANNEL, "member", ALICE, {"event_type": "user_added"})

    told = connector.render_events({"reaction": 1}, reaction).split("\n\n")
    assert told == ['[mattermost_event]\n{"emoji_name": "tada"}',
                    f"[chat_id: {CHANNEL}, message_id: {POST}, root_message_id: {ROOT}] [trigger: event]",
                    '[mattermost_events]\n{"reaction": 1}', EVENT_TURN_RANKING + REPLY_BY_TOOL]
    assert connector.render_events(None, member).endswith(EVENT_TURN_RANKING + EVENT_TURN_DESTINATION_ROOT)
    assert connector.render_events({"member": 2}, None) == '[mattermost_events]\n{"member": 2}'
