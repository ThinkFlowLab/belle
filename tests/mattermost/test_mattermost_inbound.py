"""Each row of the inbound mapping: a ``posted`` or ``post_edited`` event becomes one ``InboundMessage``.

After a gap that a new connection did not send, the posts, the member changes and the reactions of the gap are
read back and handed over as the connection would have.
"""

import asyncio
import json
import time

import pytest
from transport import BOT_USER, CONNECTION, TOKEN, URL, EventFeeder, FakeRest, hello, posted

from belle_agent.platforms.base import (
    TRIGGER_DM,
    TRIGGER_MENTION,
    TRIGGER_NONE,
    TRIGGER_REPLY,
    InboundEvent,
    InboundMessage,
)
from belle_agent.platforms.mattermost.client import MattermostClient
from belle_agent.platforms.mattermost.connector import POSTS_PAGE, MattermostPlatform
from belle_agent.platforms.mattermost.settings import MattermostSettings

POST = "p" * 26
ROOT = "r" * 26
CHANNEL = "c" * 26
TEAM = "t" * 26
ALICE = "a" * 26


class Started:
    """A started connector on the fake server, and every record it handed the host."""

    def __init__(self, rest: FakeRest) -> None:
        self.rest = rest
        self.records: list[InboundMessage] = []
        client = MattermostClient(URL, TOKEN, transport=rest.transport(), open_events=EventFeeder(hello()).open)
        self.connector = MattermostPlatform(MattermostSettings(URL, TOKEN), client=client)

    async def receive(self, record: InboundMessage) -> str:
        self.records.append(record)
        return "admitted"

    async def one(self, event) -> InboundMessage:
        assert await self.connector.handle(event) == "admitted"
        return self.records[-1]


@pytest.fixture
async def started():
    running = Started(FakeRest())
    await running.connector.start(running.receive)
    yield running
    await running.connector.stop()


async def test_a_channel_post_maps_its_ids_and_is_answered_in_its_thread(started):
    record = await started.one(posted(POST, channel=CHANNEL, user=ALICE, message="lunch?", team=TEAM))

    assert (record.platform, record.workspace, record.conversation) == ("mattermost", TEAM, CHANNEL)
    assert (record.message_id, record.event_id, record.sender, record.text) == (POST, POST, ALICE, "lunch?")
    assert (record.thread, record.reply_thread, record.chat_type, record.trigger) == ("", POST, "O", TRIGGER_NONE)
    assert record.envelope == f"[chat_id: {CHANNEL}, message_id: {POST}]"
    assert not (record.edited or record.sender_is_bot)


async def test_a_post_in_a_thread_names_its_root(started):
    started.rest.posts[ROOT] = {"id": ROOT, "user_id": ALICE}
    record = await started.one(posted(POST, root=ROOT))

    assert (record.thread, record.reply_thread, record.trigger) == (ROOT, ROOT, TRIGGER_NONE)
    assert record.envelope.endswith(f", root_message_id: {ROOT}]")


@pytest.mark.parametrize(("channel_type", "workspace"), [("O", TEAM), ("P", TEAM), ("D", "direct"), ("G", "direct")])
async def test_the_chat_type_is_the_channel_type_and_a_direct_channel_has_no_team(started, channel_type, workspace):
    record = await started.one(posted(POST, channel_type=channel_type, team="" if workspace == "direct" else TEAM))

    assert (record.chat_type, record.workspace) == (channel_type, workspace)


async def test_a_direct_message_is_a_dm_answered_where_it_was_sent(started):
    record = await started.one(posted(POST, channel_type="D", team=""))

    assert (record.trigger, record.reply_thread) == (TRIGGER_DM, "")


async def test_a_mention_of_the_bot_is_a_mention_without_the_leading_name(started):
    record = await started.one(posted(POST, message="@Belle  what is the tide?", mentions=[ALICE, BOT_USER]))

    assert (record.trigger, record.text) == (TRIGGER_MENTION, "what is the tide?")
    assert record.envelope == f"[chat_id: {CHANNEL}, message_id: {POST}] [trigger: mention]"


async def test_a_name_that_only_starts_like_the_bot_s_is_kept(started):
    record = await started.one(posted(POST, message="@belle.bis hi", mentions=[BOT_USER]))

    assert record.text == "@belle.bis hi"


async def test_a_reply_under_the_bot_s_post_is_a_reply_and_the_root_is_read_once(started):
    started.rest.posts[ROOT] = {"id": ROOT, "user_id": BOT_USER}

    first = await started.one(posted(POST, root=ROOT))
    second = await started.one(posted("q" * 26, root=ROOT))

    assert first.trigger == second.trigger == TRIGGER_REPLY
    assert first.envelope.endswith(f", root_message_id: {ROOT}] [trigger: reply]")
    assert len(started.rest.calls("GET", f"/posts/{ROOT}")) == 1


async def test_a_reply_under_a_post_the_bot_made_needs_no_read(started):
    root = await started.connector.post(CHANNEL, "", "the tide turns at four")

    assert (await started.one(posted(POST, root=root))).trigger == TRIGGER_REPLY
    assert not started.rest.calls("GET", "/posts/")


async def test_a_root_the_server_does_not_return_makes_no_reply(started):
    assert (await started.one(posted(POST, root=ROOT))).trigger == TRIGGER_NONE


async def test_an_edit_is_marked_and_carries_its_update_time_in_its_event_id(started):
    record = await started.one(posted(POST, edited=True, update_at=1800000000123))

    assert record.edited
    assert (record.message_id, record.event_id) == (POST, f"{POST}:1800000000123")


async def test_a_post_by_another_bot_says_so(started):
    assert (await started.one(posted(POST, props={"from_bot": "true"}))).sender_is_bot


async def test_the_bot_s_own_post_and_a_system_post_never_reach_the_host(started):
    assert await started.connector.handle(posted(POST, user=BOT_USER)) == "ignored:own-post"
    assert await started.connector.handle(posted(POST, type="system_join_channel")) == "ignored:post-type-system_join_channel"
    assert await started.connector.handle({"event": "typing", "data": {}, "seq": 3}) == "ignored:event-typing"
    assert not started.records


async def test_in_a_group_channel_only_the_bot_s_name_in_the_text_is_a_mention(started):
    # A group channel lists every member among the mentions of every post.
    listed = await started.one(posted(POST, channel_type="G", team="", message="lunch?", mentions=[ALICE, BOT_USER]))
    named = await started.one(posted("q" * 26, channel_type="G", team="", message="ask @belle.", mentions=[BOT_USER]))

    assert (listed.trigger, named.trigger) == (TRIGGER_NONE, TRIGGER_MENTION)


async def test_a_mention_of_everybody_does_not_mention_the_bot(started):
    # The server lists the bot among the mentions of @channel, @all and @here.
    record = await started.one(posted(POST, message="@channel lunch is ready", mentions=[BOT_USER]))

    assert record.trigger == TRIGGER_NONE


DIRECT = "d" * 26
QUIET = "q" * 26


async def _after_a_new_connection(
    rest: FakeRest, count: int, *live, settings: MattermostSettings | None = None
) -> list[InboundMessage | InboundEvent]:
    """Start a connector, hand it the ``live`` events, drop its connection, and let the server open a new one.

    Return the first ``count`` records, those of the live events among them.
    """
    records: list[InboundMessage | InboundEvent] = []

    async def receive(record):
        records.append(record)
        return "admitted"

    feeder = EventFeeder(hello(), *live, None, hello(connection="n3wn3wn3wn3wn3wn3wn3wn3wn3w"))
    client = MattermostClient(URL, TOKEN, transport=rest.transport(), open_events=feeder.open)
    connector = MattermostPlatform(settings or MattermostSettings(URL, TOKEN), client=client)
    await connector.start(receive)
    try:
        async with asyncio.timeout(10):
            while len(records) < count:
                await asyncio.sleep(0.01)
    finally:
        await connector.stop()
    return records


def _server_with_channels() -> FakeRest:
    rest = FakeRest()
    rest.channels.update({
        CHANNEL: {"id": CHANNEL, "type": "O", "team_id": TEAM},
        DIRECT: {"id": DIRECT, "type": "D", "team_id": ""},
        QUIET: {"id": QUIET, "type": "O", "team_id": TEAM},
    })
    rest.members.update({channel: [BOT_USER, ALICE] for channel in rest.channels})
    return rest


async def test_posts_a_new_connection_did_not_send_are_read_back_oldest_first():
    rest = _server_with_channels()
    now = int(time.time() * 1000)
    rest.post("o" * 26, CHANNEL, ALICE, "@belle long before the drop", create_at=now - 600_000)
    rest.post("x" * 26, QUIET, ALICE, "@belle long before the drop", create_at=now - 600_000)
    # More than one page of the channel's posts falls in the gap.
    gap = [rest.post(f"g{index:025d}", CHANNEL, ALICE, f"@belle {index}", create_at=now + index)["id"]
           for index in range(POSTS_PAGE + 1)]
    rest.post(DIRECT, DIRECT, ALICE, "are you there?", create_at=now + POSTS_PAGE + 1)

    records = await _after_a_new_connection(rest, len(gap) + 1)

    assert [record.message_id for record in records] == [*gap, DIRECT]
    assert {record.trigger for record in records[:-1]} == {TRIGGER_MENTION}
    assert (records[-1].trigger, records[-1].workspace) == (TRIGGER_DM, "direct")
    assert [call.query["page"] for call in rest.calls("GET", f"/channels/{CHANNEL}/posts")] == ["0", "1"]
    # A channel with no post since the drop is read for the reactions of its latest posts, one page by default.
    assert [call.query["page"] for call in rest.calls("GET", f"/channels/{QUIET}/posts")] == ["0"]


async def test_a_channel_whose_posts_cannot_be_read_back_leaves_the_others_read_back():
    rest = _server_with_channels()
    now = int(time.time() * 1000)
    rest.post("o" * 26, CHANNEL, ALICE, "@belle in the gap", create_at=now)
    rest.post(DIRECT, DIRECT, ALICE, "are you there?", create_at=now + 1)
    rest.routes[("GET", f"/channels/{CHANNEL}/posts")] = (500, {"id": "app_error", "status_code": 500})

    records = await _after_a_new_connection(rest, 1)

    assert [record.message_id for record in records] == [DIRECT]


ADMIN = "m" * 26
BOB = "o" * 26
CAROL = "k" * 26


def _member_post(kind: str, user: str, created: int, **props: str) -> dict:
    """A system post of a member change, as the server keeps it: who it names is in ``user_id`` or the props."""
    return {"user": user, "type": kind, "props": props, "create_at": created}


def _summary(record: InboundMessage | InboundEvent) -> tuple[str, ...]:
    if isinstance(record, InboundEvent):
        return (record.record["event_type"], record.actor, record.conversation)
    return ("posted", record.message_id)


async def test_member_changes_of_a_gap_are_read_back_as_member_events_in_order_with_its_posts():
    rest = _server_with_channels()
    now = int(time.time() * 1000)
    # Each kind of member change, as Mattermost 12.0.1 posts it. A team's own changes are posted in its default
    # channel, whose members are the team's.
    changes = [
        ("system_add_to_channel", ADMIN, {"addedUserId": ALICE, "userId": ADMIN}, ("user_added", ALICE)),
        ("system_join_channel", BOB, {"username": "bob"}, ("user_added", BOB)),
        ("system_remove_from_channel", ADMIN, {"removedUserId": ALICE, "removedUsername": "alice"}, ("user_removed", ALICE)),
        ("system_leave_channel", BOB, {"username": "bob"}, ("user_removed", BOB)),
        ("system_add_to_team", ADMIN, {"addedUserId": CAROL, "userId": ADMIN}, ("user_added", CAROL)),
        ("system_join_team", CAROL, {"username": "carol"}, ("user_added", CAROL)),
        ("system_remove_from_team", CAROL, {"username": "carol"}, ("user_removed", CAROL)),
        ("system_leave_team", CAROL, {"username": "carol"}, ("user_removed", CAROL)),
    ]
    ids = [f"s{index:025d}" for index in range(len(changes))]
    for index, (post_id, (kind, user, props, _)) in enumerate(zip(ids, changes, strict=True)):
        rest.post(post_id, CHANNEL, **_member_post(kind, user, now + 2 * index, **props))
    # A person's post in the middle, and one after them all.
    rest.post("y" * 26, CHANNEL, ALICE, "@belle between", create_at=now + 5)
    rest.post(DIRECT, DIRECT, ALICE, "are you there?", create_at=now + 100)

    records = await _after_a_new_connection(rest, len(changes) + 2)

    expected = [(kind, actor, CHANNEL) for *_, (kind, actor) in changes]
    assert [_summary(record) for record in records] == [*expected[:3], ("posted", "y" * 26), *expected[3:], ("posted", DIRECT)]
    events = [record for record in records if isinstance(record, InboundEvent)]
    assert {event.kind for event in events} == {"member"}
    # The system post is the change's own record: the host drops a change read back twice by its id.
    assert [event.event_id for event in events] == ids


async def test_a_system_post_that_changes_no_member_is_not_read_back():
    rest = _server_with_channels()
    now = int(time.time() * 1000)
    for index, kind in enumerate(("system_header_change", "system_purpose_change", "system_displayname_change")):
        rest.post(f"h{index:025d}", CHANNEL, **_member_post(kind, ADMIN, now + index))
    rest.post(DIRECT, DIRECT, ALICE, "are you there?", create_at=now + 10)

    records = await _after_a_new_connection(rest, 1)

    assert [_summary(record) for record in records] == [("posted", DIRECT)]


def _member_event(kind: str, user: str, seq: int) -> dict:
    return {"event": kind, "data": {"user_id": user, "team_id": TEAM}, "broadcast": {"channel_id": CHANNEL}, "seq": seq}


async def test_a_member_change_the_connection_delivered_before_the_drop_is_handed_over_once():
    rest = _server_with_channels()
    now = int(time.time() * 1000)
    # The connection delivered Alice's arrival, and the read-back reaches back to before it. She then left and came
    # back in the gap: those two changes are new.
    rest.post("s" * 26, CHANNEL, **_member_post("system_add_to_channel", ADMIN, now - 1000, addedUserId=ALICE))
    rest.post("u" * 26, CHANNEL, **_member_post("system_leave_channel", ALICE, now + 1))
    rest.post("v" * 26, CHANNEL, **_member_post("system_join_channel", ALICE, now + 2))
    rest.post(DIRECT, DIRECT, ALICE, "are you there?", create_at=now + 10)

    records = await _after_a_new_connection(rest, 4, _member_event("user_added", ALICE, seq=1))

    assert [_summary(record) for record in records] == [
        ("user_added", ALICE, CHANNEL), ("user_removed", ALICE, CHANNEL), ("user_added", ALICE, CHANNEL), ("posted", DIRECT),
    ]


def _reaction(user: str, emoji: str, created: int) -> dict:
    return {"user_id": user, "post_id": "", "emoji_name": emoji, "create_at": created, "update_at": created, "delete_at": 0}


async def test_a_reaction_of_a_gap_on_a_post_it_reads_back_is_a_reaction_event_about_that_post():
    rest = _server_with_channels()
    now = int(time.time() * 1000)
    since = now - 60_000
    reactions = [
        _reaction(ALICE, "tada", now + 3),
        # The bot's own marks are no news to it, and a reaction from before the read-back's start is not of the gap.
        _reaction(BOT_USER, "eyes", now + 2),
        _reaction(BOB, "wave", since - 1),
    ]
    rest.post("g" * 26, CHANNEL, ALICE, "@belle in the gap", create_at=now + 1, metadata={"reactions": reactions})
    rest.post(DIRECT, DIRECT, ALICE, "are you there?", create_at=now + 10)

    records = await _after_a_new_connection(rest, 3)

    assert [_summary(record) for record in records] == [
        ("posted", "g" * 26), ("reaction_added", ALICE, CHANNEL), ("posted", DIRECT),
    ]
    reaction = records[1]
    assert isinstance(reaction, InboundEvent)
    assert (reaction.kind, reaction.anchor, reaction.record["emoji_name"], reaction.record["message_id"]) == (
        "reaction", "g" * 26, "tada", "g" * 26
    )


async def test_a_reaction_has_the_same_event_id_live_and_read_back_so_the_host_hands_it_over_once():
    rest = _server_with_channels()
    now = int(time.time() * 1000)
    reaction = {**_reaction(ALICE, "tada", now - 1000), "post_id": "g" * 26, "channel_id": CHANNEL}
    rest.post("g" * 26, CHANNEL, ALICE, "@belle before the drop", create_at=now - 2000, metadata={"reactions": [reaction]})
    rest.post(DIRECT, DIRECT, ALICE, "are you there?", create_at=now + 10)
    live = {"event": "reaction_added", "data": {"reaction": json.dumps(reaction)}, "broadcast": {"channel_id": CHANNEL}, "seq": 1}

    records = await _after_a_new_connection(rest, 3, live)

    delivered, read_back = [record for record in records if isinstance(record, InboundEvent)]
    assert delivered.event_id == read_back.event_id
    # The id names the reaction, not the connection that carried it.
    assert "n3wn3w" not in read_back.event_id and CONNECTION not in delivered.event_id


async def test_a_reaction_of_a_gap_on_an_older_post_is_read_back_in_a_channel_with_no_new_post():
    rest = _server_with_channels()
    now = int(time.time() * 1000)
    # A reaction moves no channel's last post time, so the quiet channel is read too.
    reaction = _reaction(ALICE, "tada", now + 1)
    rest.post("x" * 26, QUIET, BOT_USER, "an answer from long ago", create_at=now - 600_000, metadata={"reactions": [reaction]})
    rest.post(DIRECT, DIRECT, ALICE, "are you there?", create_at=now + 10)

    records = await _after_a_new_connection(rest, 2)

    assert [_summary(record) for record in records] == [("reaction_added", ALICE, QUIET), ("posted", DIRECT)]
    assert records[0].anchor == "x" * 26


@pytest.mark.parametrize(("pages", "read_back"), [(1, False), (2, True)])
async def test_the_reactions_of_a_channel_s_latest_posts_are_read_back_as_far_as_the_operator_sets(pages, read_back):
    rest = _server_with_channels()
    now = int(time.time() * 1000)
    old = now - 600_000
    # The reacted post is the newest of the channel's second page of posts, all from before the gap.
    for index in range(2 * POSTS_PAGE):
        reactions = [_reaction(ALICE, "tada", now + 1)] if index == POSTS_PAGE - 1 else []
        rest.post(f"o{index:025d}", QUIET, BOB, str(index), create_at=old + index, metadata={"reactions": reactions})
    rest.post(DIRECT, DIRECT, ALICE, "are you there?", create_at=now + 10)
    settings = MattermostSettings(URL, TOKEN, gap_reaction_scan_max_pages=pages)

    records = await _after_a_new_connection(rest, 1 + read_back, settings=settings)

    expected = [("reaction_added", ALICE, QUIET)] if read_back else []
    assert [_summary(record) for record in records] == [*expected, ("posted", DIRECT)]
    assert [call.query["page"] for call in rest.calls("GET", f"/channels/{QUIET}/posts")] == [str(page) for page in range(pages)]


async def test_a_member_change_delivered_long_before_a_gap_does_not_hide_the_same_change_in_it():
    rest = _server_with_channels()
    started = Started(rest)
    await started.connector.start(started.receive)
    try:
        assert await started.connector.handle(_member_event("user_added", ALICE, seq=1)) == "admitted"
        # Ten minutes later the connection drops, and Alice is added again in the gap.
        dropped_at = time.time() + 600
        rest.post("s" * 26, CHANNEL, **_member_post("system_add_to_channel", ADMIN, int(dropped_at * 1000) + 1, addedUserId=ALICE))

        await started.connector.socket._on_gap(dropped_at, dropped_at + 5)
    finally:
        await started.connector.stop()

    assert [_summary(record) for record in started.records] == [("user_added", ALICE, CHANNEL)] * 2
