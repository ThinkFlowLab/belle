"""The Discord connector: messages in, posts, threads, reactions, marks and files out, on a Discord in this process."""

import asyncio
import base64

import pytest
from discord_transport import TOKEN, FakeGateway, FakeRest, hello, ready

from belle_agent.backend import Question
from belle_agent.config import BelleConfigError
from belle_agent.platforms.base import (
    MARK_DONE,
    MARK_RECEIVED,
    TRIGGER_DM,
    TRIGGER_MENTION,
    TRIGGER_NONE,
    TRIGGER_REPLY,
    InboundEvent,
)
from belle_agent.platforms.discord.client import DiscordClient, DiscordError
from belle_agent.platforms.discord.connector import DiscordPlatform, create_platform
from belle_agent.platforms.discord.render import ALLOWED_MENTIONS
from belle_agent.platforms.discord.settings import TOKEN_ENV, DiscordSettings

BOT = "1000000000000000116"
GUILD = "1000000000000000200"
BOT_ROLE = "1000000000000000201"
OTHER_ROLE = "1000000000000000202"
CHANNEL = "1000000000000000300"
THREAD = "1000000000000000400"
DM = "1000000000000000500"
GROUP_DM = "1000000000000000600"
ALICE = "1000000000000000700"
MESSAGE = "1000000000000000800"
CDN = "https://cdn.discordapp.com/attachments/1/2/notes.md"


def _message(text="hello", *, channel=CHANNEL, message=MESSAGE, author=ALICE, mentions=(), **fields):
    data = {"id": message, "channel_id": channel, "author": {"id": author, "username": "alice"}, "content": text,
            "mentions": [{"id": user} for user in mentions], "type": 0, **fields}
    if channel != DM:
        data.setdefault("guild_id", GUILD)
    return {"op": 0, "s": 9, "t": "MESSAGE_CREATE", "d": data}


GUILD_CREATE = {"op": 0, "s": 2, "t": "GUILD_CREATE", "d": {
    "id": GUILD,
    "channels": [{"id": CHANNEL, "type": 0}],
    "threads": [{"id": THREAD, "type": 11, "parent_id": CHANNEL, "owner_id": BOT}],
}}


@pytest.fixture
def rest():
    found = FakeRest()
    found.gateway()
    found.answer("GET", f"/users/{ALICE}", (200, {"id": ALICE}))
    found.answer("GET", f"/channels/{DM}", (200, {"id": DM, "type": 1}))
    found.answer("GET", f"/channels/{GROUP_DM}", (200, {"id": GROUP_DM, "type": 3}))
    return found


@pytest.fixture
async def connected(rest):
    started = []

    async def connect(gateway=None, **settings):
        gateway = gateway or FakeGateway(_opens(ready()))
        client = DiscordClient(TOKEN, transport=rest.transport(), open_socket=gateway.open)
        platform = DiscordPlatform(DiscordSettings(token=TOKEN, **settings), client=client)
        received = []

        async def on_inbound(record):
            received.append(record)
            return "dispatched:test"

        await platform.start(on_inbound)
        await platform.handle(GUILD_CREATE)
        started.append(platform)
        return platform, received

    yield connect
    for platform in started:
        await platform.stop()


def _opens(*payloads):
    def script(socket):
        socket.feed(hello(41250))
        for payload in payloads:
            socket.feed(payload)
    return script


async def test_start_waits_for_ready_and_knows_the_bot(connected):
    platform, _ = await connected()
    assert (platform.bot_user_id, platform.bot_username) == (BOT, "belle")


async def test_a_privileged_intent_left_off_refuses_the_start_naming_the_toggle(connected):
    def refuses(socket):
        socket.feed(hello(41250))
        socket.end(4014)

    with pytest.raises(BelleConfigError, match="turn on Message Content"):
        await connected(FakeGateway(refuses))


async def test_a_mention_in_a_channel_is_answered_in_a_thread_started_on_the_message(connected):
    platform, received = await connected()

    assert await platform.handle(_message(f"<@{BOT}> what is 2+2?", mentions=[BOT])) == "dispatched:test"

    (inbound,) = received
    assert (inbound.workspace, inbound.conversation, inbound.thread, inbound.reply_thread) == (GUILD, CHANNEL, "", MESSAGE)
    assert (inbound.trigger, inbound.text, inbound.chat_type) == (TRIGGER_MENTION, "what is 2+2?", "text")
    assert inbound.envelope == f"[chat_id: {CHANNEL}, message_id: {MESSAGE}] [trigger: mention]"


def _role_mention(role, message):
    return _message(f"<@&{role}> help", message=message, mention_roles=[role])


def _member_update(user, roles, sequence=30):
    return {"op": 0, "s": sequence, "t": "GUILD_MEMBER_UPDATE", "d": {
        "guild_id": GUILD, "user": {"id": user}, "roles": roles}}


async def test_a_role_mention_addresses_the_bot_by_the_roles_its_guild_create_named(connected, rest):
    platform, received = await connected()
    await platform.handle({**GUILD_CREATE, "d": {**GUILD_CREATE["d"], "members": [
        {"user": {"id": ALICE}, "roles": [OTHER_ROLE]}, {"user": {"id": BOT}, "roles": [BOT_ROLE]}]}})

    await platform.handle(_role_mention(BOT_ROLE, "1000000000000000801"))
    await platform.handle(_role_mention(OTHER_ROLE, "1000000000000000802"))

    assert [inbound.trigger for inbound in received] == [TRIGGER_MENTION, TRIGGER_NONE]
    assert rest.calls("GET", f"/guilds/{GUILD}/members/{BOT}") == []


async def test_the_bot_s_roles_follow_its_member_updates_and_no_one_else_s(connected, rest):
    platform, received = await connected()
    await platform.handle({**GUILD_CREATE, "d": {**GUILD_CREATE["d"], "members": [
        {"user": {"id": BOT}, "roles": [BOT_ROLE]}]}})

    assert await platform.handle(_member_update(BOT, [OTHER_ROLE])) == "learned:bot-roles"
    assert await platform.handle(_member_update(ALICE, [BOT_ROLE], 31)) == "ignored:event-GUILD_MEMBER_UPDATE"
    await platform.handle(_role_mention(BOT_ROLE, "1000000000000000801"))
    await platform.handle(_role_mention(OTHER_ROLE, "1000000000000000802"))

    assert [inbound.trigger for inbound in received] == [TRIGGER_NONE, TRIGGER_MENTION]
    assert rest.calls("GET", f"/guilds/{GUILD}/members/{BOT}") == []


async def test_a_guild_that_did_not_name_the_bot_s_member_is_read_once(connected, rest):
    platform, received = await connected()
    rest.answer("GET", f"/guilds/{GUILD}/members/{BOT}", (200, {"roles": [BOT_ROLE]}))

    await platform.handle(_role_mention(BOT_ROLE, "1000000000000000801"))
    await platform.handle(_role_mention(OTHER_ROLE, "1000000000000000802"))
    assert len(rest.calls("GET", f"/guilds/{GUILD}/members/{BOT}")) == 1

    assert await platform.handle({"op": 0, "s": 40, "t": "GUILD_DELETE", "d": {"id": GUILD}}) == "forgot:guild"
    await platform.handle(_role_mention(BOT_ROLE, "1000000000000000803"))

    assert [inbound.trigger for inbound in received] == [TRIGGER_MENTION, TRIGGER_NONE, TRIGGER_MENTION]
    assert len(rest.calls("GET", f"/guilds/{GUILD}/members/{BOT}")) == 2


async def test_a_bot_role_lookup_that_fails_is_tried_again(connected, rest):
    platform, received = await connected()
    rest.answer("GET", f"/guilds/{GUILD}/members/{BOT}", (404, {}))

    await platform.handle(_role_mention(BOT_ROLE, "1000000000000000801"))
    rest.answer("GET", f"/guilds/{GUILD}/members/{BOT}", (200, {"roles": [BOT_ROLE]}))
    await platform.handle(_role_mention(BOT_ROLE, "1000000000000000802"))

    assert [inbound.trigger for inbound in received] == [TRIGGER_NONE, TRIGGER_MENTION]


async def test_a_message_in_the_bot_s_thread_is_a_reply_in_that_thread_of_the_parent_channel(connected):
    platform, received = await connected()

    await platform.handle(_message("and 3+3?", channel=THREAD))

    (inbound,) = received
    assert (inbound.conversation, inbound.thread, inbound.reply_thread, inbound.trigger) == (CHANNEL, THREAD, THREAD, TRIGGER_REPLY)
    assert inbound.chat_type == "thread"


async def test_a_dm_belongs_to_no_guild_and_is_answered_where_it_is(connected):
    platform, received = await connected()

    await platform.handle(_message("hi", channel=DM))

    (inbound,) = received
    assert (inbound.workspace, inbound.conversation, inbound.reply_thread, inbound.trigger, inbound.chat_type) == (
        "direct", DM, "", TRIGGER_DM, "dm"
    )


@pytest.mark.parametrize(
    ("event", "outcome"),
    [
        (_message("me", author=BOT), "ignored:own-post"),
        (_message("joined", type=7), "ignored:message-type-7"),
        (_message("hi all", channel=GROUP_DM), "ignored:group-dm"),
        (_message(""), "ignored:empty-message"),
    ],
)
async def test_what_is_no_person_s_message_to_the_bot_is_not_handed_over(connected, event, outcome):
    platform, received = await connected()

    assert await platform.handle(event) == outcome and received == []


async def test_an_app_s_message_is_handed_over_as_one_for_the_host_to_decide(connected):
    platform, received = await connected()

    await platform.handle({**_message(), "d": {**_message()["d"], "author": {"id": "1000000000000000999", "bot": True}}})
    await platform.handle(_message("hook", message="1000000000000000801", webhook_id="1"))

    assert [inbound.sender_is_bot for inbound in received] == [True, True]


async def test_an_empty_message_without_the_message_content_intent_is_named_as_such(connected):
    platform, received = await connected(message_content=False)

    assert await platform.handle(_message("")) == "ignored:no-message-content" and received == []


async def test_an_edit_carries_its_text_and_a_delivery_id_of_its_own(connected):
    platform, received = await connected()
    update = _message("fixed", edited_timestamp="2026-10-19T10:00:00")
    update["t"] = "MESSAGE_UPDATE"

    await platform.handle(update)
    assert await platform.handle({"op": 0, "s": 10, "t": "MESSAGE_UPDATE", "d": {"id": MESSAGE, "channel_id": CHANNEL,
                                  "author": {"id": ALICE}}}) == "ignored:update-without-content"

    (inbound,) = received
    assert inbound.edited and inbound.event_id == f"{MESSAGE}:2026-10-19T10:00:00"


async def test_with_reply_in_thread_off_a_channel_message_is_answered_in_the_channel(connected):
    platform, received = await connected(reply_in_thread=False)

    await platform.handle(_message(f"<@{BOT}> hi", mentions=[BOT]))

    assert received[0].reply_thread == ""


async def test_posts_into_a_new_thread_start_it_once_and_notify_only_users(connected, rest):
    platform, _ = await connected()
    await platform.handle(_message(f"<@{BOT}> what is 2+2?", mentions=[BOT]))
    rest.answer("POST", f"/channels/{CHANNEL}/messages/{MESSAGE}/threads", (201, {"id": MESSAGE, "type": 11, "parent_id": CHANNEL}))
    rest.answer("POST", f"/channels/{MESSAGE}/messages", (200, {"id": "1000000000000000901", "channel_id": MESSAGE}))

    await asyncio.gather(platform.post(CHANNEL, MESSAGE, "4"), platform.post(CHANNEL, MESSAGE, "@everyone 4"))

    (started,) = rest.calls("POST", f"/channels/{CHANNEL}/messages/{MESSAGE}/threads")
    assert started.body == {"name": "what is 2+2?"}
    posts = rest.calls("POST", f"/channels/{MESSAGE}/messages")
    assert [post.body["content"] for post in posts] == ["4", "@everyone 4"]
    assert all(post.body["allowed_mentions"] == ALLOWED_MENTIONS for post in posts)


async def test_a_thread_that_already_exists_is_posted_into(connected, rest):
    platform, _ = await connected()
    rest.answer("POST", f"/channels/{CHANNEL}/messages/{MESSAGE}/threads", (400, {"code": 160004}))
    rest.answer("GET", f"/channels/{MESSAGE}", (200, {"id": MESSAGE, "type": 11, "parent_id": CHANNEL}))
    rest.answer("POST", f"/channels/{MESSAGE}/messages", (200, {"id": "1000000000000000901", "channel_id": MESSAGE}))

    assert await platform.post(CHANNEL, MESSAGE, "4") == "1000000000000000901"


async def test_a_long_reply_goes_in_messages_of_at_most_2000_characters(connected, rest):
    platform, _ = await connected()
    rest.answer("POST", f"/channels/{CHANNEL}/messages", (200, {"id": "1000000000000000901", "channel_id": CHANNEL}))

    await platform.post(CHANNEL, "", " ".join(f"word{n:04d}" for n in range(600)))

    parts = [post.body["content"] for post in rest.calls("POST", f"/channels/{CHANNEL}/messages")]
    assert len(parts) > 1 and all(len(part) <= 2000 for part in parts)


async def test_a_post_for_one_person_is_refused(connected):
    platform, _ = await connected()

    with pytest.raises(DiscordError, match="ephemeral_unsupported"):
        await platform.post(CHANNEL, "", "only you", ephemeral_to=ALICE)


async def test_marks_react_in_the_message_s_own_channel_and_type_while_the_turn_runs(connected, rest):
    platform, received = await connected()
    await platform.handle(_message("in the thread", channel=THREAD))
    (inbound,) = received

    await platform.mark(inbound, MARK_RECEIVED)
    await asyncio.sleep(0.01)
    await platform.mark(inbound, MARK_DONE)

    assert [request.path for request in rest.requests if request.method == "PUT"] == [
        f"/channels/{THREAD}/messages/{MESSAGE}/reactions/\N{EYES}/@me"
    ]
    assert rest.calls("POST", f"/channels/{THREAD}/typing") and platform.typing._tasks == {}


async def test_files_come_from_the_cdn_without_the_token_and_others_are_left_out_with_a_notice(connected, rest):
    platform, received = await connected()
    rest.answer("GET", "/attachments/1/2/notes.md", (200, b"# notes"))
    rest.answer("POST", f"/channels/{CHANNEL}/messages/{MESSAGE}/threads", (201, {"id": MESSAGE, "type": 11, "parent_id": CHANNEL}))
    rest.answer("POST", f"/channels/{MESSAGE}/messages", (200, {"id": "1000000000000000901", "channel_id": MESSAGE}))
    attachments = [
        {"id": "1", "filename": "notes.md", "size": 7, "url": CDN, "content_type": "text/markdown"},
        {"id": "2", "filename": "evil.md", "size": 7, "url": "https://example.invalid/evil.md"},
        {"id": "3", "filename": "huge.bin", "size": 10**9, "url": CDN},
    ]
    await platform.handle(_message("files", attachments=attachments))

    loaded = await platform.fetch_files(received[0])

    assert loaded == ({"name": "notes.md", "uri": "data:text/markdown;base64," + base64.b64encode(b"# notes").decode()},)
    (download,) = [request for request in rest.requests if request.path == "/attachments/1/2/notes.md"]
    assert "authorization" not in download.headers
    # The notice goes where the answer goes: the thread started on the message.
    notices = [post.body["content"] for post in rest.calls("POST", f"/channels/{MESSAGE}/messages")]
    assert len(notices) == 2 and any("evil.md" in notice for notice in notices) and any("huge.bin" in notice for notice in notices)


@pytest.mark.parametrize("kind,conversation", [(11, CHANNEL), (12, THREAD)])
async def test_a_reaction_on_a_thread_message_is_an_event_anchored_in_that_thread(connected, kind, conversation):
    platform, received = await connected()

    await platform.handle({"t": "THREAD_UPDATE", "d": {"id": THREAD, "type": kind, "parent_id": CHANNEL}})
    await platform.handle({"op": 0, "s": 11, "t": "MESSAGE_REACTION_ADD", "d": {
        "user_id": ALICE, "channel_id": THREAD, "message_id": MESSAGE, "guild_id": GUILD, "emoji": {"name": "👍"}}})

    (event,) = received
    assert isinstance(event, InboundEvent)
    assert (event.conversation, event.anchor, event.anchor_thread, event.record["emoji"]) == (conversation, MESSAGE, THREAD, "👍")


async def test_a_private_thread_is_its_own_conversation_and_keeps_its_reply_target(connected, rest):
    platform, received = await connected()
    await platform.handle({"t": "THREAD_UPDATE", "d": {"id": THREAD, "type": 12, "parent_id": CHANNEL}})

    await platform.handle(_message("private", channel=THREAD, mentions=(BOT,)))

    (inbound,) = received
    assert (inbound.conversation, inbound.thread, inbound.reply_thread) == (THREAD, THREAD, THREAD)
    assert inbound.chat_type == "private_thread"
    assert f"chat_id: {THREAD}" in inbound.envelope
    assert await platform._conversation_of(THREAD) == (THREAD, "private_thread")
    rest.answer("POST", f"/channels/{THREAD}/messages", (200, {"id": "1000000000000000901", "channel_id": THREAD}))
    await platform.post(inbound.conversation, inbound.reply_thread, "answer")
    assert rest.calls("POST", f"/channels/{THREAD}/messages")
    assert not rest.calls("POST", f"/channels/{CHANNEL}/messages")


async def test_the_metadata_of_a_turn_names_its_channel_thread_message_sender_and_guild(connected):
    platform, received = await connected()
    await platform.handle(_message("x", channel=THREAD))

    assert platform.request_metadata(received[0]) == {
        "discord_channel_id": CHANNEL, "discord_channel_type": "thread", "discord_message_id": MESSAGE,
        "discord_thread_id": THREAD, "discord_reply_thread_id": THREAD, "discord_user_id": ALICE,
        "discord_guild_id": GUILD, "discord_bot": "",
    }


async def test_decision_facts_use_the_shared_contract_and_name_the_discord_bot(connected):
    platform, received = await connected()
    await platform.handle(_message("hello", channel=DM))

    facts = platform.decision_facts(received[0])
    assert facts == {
        "channel": DM,
        "bot_name": f"belle (mentioned as <@{BOT}>)",
        "last_message": {
            "author": ALICE,
            "text": "hello",
            "ts": MESSAGE,
            "thread_ts": "",
            "thread_position": "root",
            "file_count": 0,
        },
        "conversation_kind": "one_to_one_dm",
        "identity": {
            "workspace": "direct",
            "channel": DM,
            "ts": MESSAGE,
            "thread_ts": "",
            "user": ALICE,
            "chat_type": "dm",
            "is_dm": True,
            "trigger": TRIGGER_DM,
        },
    }


def test_the_platform_is_built_from_its_section_with_the_token_from_the_environment(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    platform = create_platform({"platforms": {"discord": {"reply_in_thread": False}}})
    assert isinstance(platform, DiscordPlatform) and platform._settings.reply_in_thread is False


async def test_typing_shows_in_the_message_s_channel_until_the_answer_starts_its_thread(connected, rest, monkeypatch):
    from belle_agent.platforms.discord import typing_indicator

    monkeypatch.setattr(typing_indicator, "REPEAT_SECONDS", 0.01)
    # Discord refuses a notice in a thread that does not exist yet, as it did live: 404, Unknown Channel.
    rest.answer("POST", f"/channels/{MESSAGE}/typing", (404, {"code": 10003, "message": "Unknown Channel"}))
    rest.answer("POST", f"/channels/{CHANNEL}/messages/{MESSAGE}/threads", (201, {"id": MESSAGE, "type": 11, "parent_id": CHANNEL}))
    rest.answer("POST", f"/channels/{MESSAGE}/messages", (200, {"id": "1000000000000000901", "channel_id": MESSAGE}))
    platform, received = await connected()
    await platform.handle(_message(f"<@{BOT}> hi", mentions=[BOT]))
    (inbound,) = received

    await platform.mark(inbound, MARK_RECEIVED)
    await asyncio.sleep(0.03)
    assert rest.calls("POST", f"/channels/{CHANNEL}/typing") and not rest.calls("POST", f"/channels/{MESSAGE}/typing")

    rest.answer("POST", f"/channels/{MESSAGE}/typing", (204, {}))
    await platform.post(CHANNEL, MESSAGE, "4")
    await asyncio.sleep(0.03)
    await platform.mark(inbound, MARK_DONE)

    assert rest.calls("POST", f"/channels/{MESSAGE}/typing") and platform.typing._tasks == {}


async def test_typing_for_a_message_inside_a_thread_shows_in_that_thread(connected, rest):
    platform, received = await connected()
    await platform.handle(_message("and 3+3?", channel=THREAD))

    await platform.mark(received[0], MARK_RECEIVED)
    await asyncio.sleep(0.01)
    await platform.mark(received[0], MARK_DONE)

    assert rest.calls("POST", f"/channels/{THREAD}/typing") and not rest.calls("POST", f"/channels/{CHANNEL}/typing")


async def test_fatal_gateway_close_reports_down_and_stops_typing(connected):
    platform, received = await connected()
    await platform.handle(_message())
    await platform.mark(received[0], MARK_RECEIVED)
    platform.gateway._socket.end(4014)
    assert "4014" in await asyncio.wait_for(platform.wait_down(), 1)
    assert platform.typing._tasks == {}
    assert platform.receiver is None


async def test_unexpected_reader_failure_reports_only_exception_type(connected, monkeypatch, rest):
    platform, _ = await connected()
    question_message = "1000000000000000902"
    rest.answer("POST", f"/channels/{THREAD}/messages", (200, {"id": question_message, "channel_id": THREAD}))
    await platform.ask(CHANNEL, THREAD, Question("q-reader-close", "Still there?", ("yes",)), workspace=GUILD)

    async def broken():
        raise RuntimeError("secret arbitrary response")
        yield

    platform._reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await platform._reader
    monkeypatch.setattr(platform.gateway, "events", broken)
    platform._reader = asyncio.create_task(platform._read())
    assert await asyncio.wait_for(platform.wait_down(), 1) == "discord gateway reader failed (RuntimeError)"
    assert rest.calls("PATCH", f"/channels/{THREAD}/messages/{question_message}")[-1].body == {"components": []}


async def test_reaction_identity_distinguishes_actions_and_preserves_replays_and_sessions(connected):
    platform, received = await connected()
    event = {"op": 0, "s": 11, "t": "MESSAGE_REACTION_ADD", "d": {
        "user_id": ALICE, "channel_id": CHANNEL, "message_id": MESSAGE, "guild_id": GUILD, "emoji": {"name": "👍"}}}
    await platform.handle(event)
    await platform.handle({**event, "s": 12, "t": "MESSAGE_REACTION_REMOVE"})
    await platform.handle({**event, "s": 13})
    await platform.handle(event)
    assert received[0].event_id == received[3].event_id
    assert len({record.event_id for record in received}) == 3
    platform.gateway.session_id = "new-session"
    await platform.handle(event)
    assert received[-1].event_id != received[0].event_id


async def test_member_event_without_system_channel_is_dropped(connected, rest, caplog):
    platform, received = await connected()
    rest.answer("GET", f"/guilds/{GUILD}", (200, {"id": GUILD, "system_channel_id": None}))
    result = await platform.handle({"op": 0, "s": 20, "t": "GUILD_MEMBER_ADD", "d": {
        "guild_id": GUILD, "user": {"id": ALICE}}})
    assert result == "dropped:no-usable-member-destination" and not received
    assert "no system channel or configured member-event channel" in caplog.text


async def test_member_event_uses_configured_channel_when_system_channel_is_absent(connected, rest):
    platform, received = await connected(member_event_channels={GUILD: CHANNEL})
    permissions = (1 << 10) | (1 << 11)
    rest.answer("GET", f"/guilds/{GUILD}", (200, {"id": GUILD, "system_channel_id": None,
        "owner_id": ALICE, "roles": [{"id": GUILD, "permissions": str(permissions)}]}))
    rest.answer("GET", f"/guilds/{GUILD}/members/{BOT}", (200, {"user": {"id": BOT}, "roles": []}))
    rest.answer("GET", f"/channels/{CHANNEL}", (200, {"id": CHANNEL, "guild_id": GUILD, "type": 0}))

    result = await platform.handle({"op": 0, "s": 20, "t": "GUILD_MEMBER_ADD", "d": {
        "guild_id": GUILD, "user": {"id": ALICE}}})

    assert result == "dispatched:test"
    assert received[0].conversation == CHANNEL


async def test_member_event_falls_back_when_system_channel_is_not_postable(connected, rest):
    platform, received = await connected(member_event_channels={GUILD: THREAD})
    permissions = (1 << 10) | (1 << 11)
    rest.answer("GET", f"/guilds/{GUILD}", (200, {"id": GUILD, "system_channel_id": CHANNEL,
        "owner_id": ALICE, "roles": [{"id": GUILD, "permissions": str(permissions)}]}))
    rest.answer("GET", f"/guilds/{GUILD}/members/{BOT}", (200, {"user": {"id": BOT}, "roles": []}))
    rest.answer("GET", f"/channels/{CHANNEL}", (200, {"id": CHANNEL, "guild_id": GUILD, "type": 0,
        "permission_overwrites": [{"id": BOT, "type": 1, "deny": str(1 << 11), "allow": "0"}]}))
    rest.answer("GET", f"/channels/{THREAD}", (200, {"id": THREAD, "guild_id": GUILD, "type": 5}))

    result = await platform.handle({"op": 0, "s": 20, "t": "GUILD_MEMBER_ADD", "d": {
        "guild_id": GUILD, "user": {"id": ALICE}}})

    assert result == "dispatched:test"
    assert received[0].conversation == THREAD


async def test_member_event_drops_configured_channel_from_another_guild(connected, rest):
    platform, received = await connected(member_event_channels={GUILD: CHANNEL})
    rest.answer("GET", f"/guilds/{GUILD}", (200, {"id": GUILD, "system_channel_id": None,
        "owner_id": ALICE, "roles": [{"id": GUILD, "permissions": "0"}]}))
    rest.answer("GET", f"/guilds/{GUILD}/members/{BOT}", (200, {"user": {"id": BOT}, "roles": []}))
    rest.answer("GET", f"/channels/{CHANNEL}", (200, {"id": CHANNEL, "guild_id": "1000000000000000999", "type": 0}))

    result = await platform.handle({"op": 0, "s": 20, "t": "GUILD_MEMBER_ADD", "d": {
        "guild_id": GUILD, "user": {"id": ALICE}}})

    assert result == "dropped:no-usable-member-destination"
    assert received == []


@pytest.mark.parametrize("channel", [
    {"id": "1000000000000000998", "guild_id": "1000000000000000999", "type": 0},
    {"id": "1000000000000000998", "guild_id": GUILD, "type": 2},
])
async def test_member_event_with_invalid_system_channel_is_dropped(connected, rest, channel):
    platform, received = await connected()
    channel_id = channel["id"]
    rest.answer("GET", f"/guilds/{GUILD}", (200, {"id": GUILD, "owner_id": ALICE, "roles": [
        {"id": GUILD, "permissions": "0"}], "system_channel_id": channel_id}))
    rest.answer("GET", f"/guilds/{GUILD}/members/{BOT}", (200, {"user": {"id": BOT}, "roles": []}))
    rest.answer("GET", f"/channels/{channel_id}", (200, channel))

    result = await platform.handle({"op": 0, "s": 20, "t": "GUILD_MEMBER_ADD", "d": {
        "guild_id": GUILD, "user": {"id": ALICE}}})

    assert result == "dropped:no-usable-member-destination"
    assert received == []
    assert channel_id not in platform._channels


@pytest.mark.parametrize("denied", [False, True])
async def test_member_event_uses_only_postable_system_channel(connected, rest, denied):
    platform, received = await connected()
    permissions = (1 << 10) | (1 << 11)
    rest.answer("GET", f"/guilds/{GUILD}", (200, {"id": GUILD, "system_channel_id": CHANNEL,
        "owner_id": ALICE, "roles": [{"id": GUILD, "permissions": str(permissions)}]}))
    rest.answer("GET", f"/guilds/{GUILD}/members/{BOT}", (200, {"user": {"id": BOT}, "roles": []}))
    rest.answer("GET", f"/channels/{CHANNEL}", (200, {"id": CHANNEL, "guild_id": GUILD, "type": 0,
        "permission_overwrites": [{"id": BOT, "type": 1, "deny": str(1 << 11), "allow": "0"}] if denied else []}))
    event = {"op": 0, "s": 20, "t": "GUILD_MEMBER_ADD", "d": {"guild_id": GUILD, "user": {"id": ALICE}}}
    await platform.handle(event)
    if denied:
        assert received == []
    else:
        await platform.handle({**event, "s": 21, "t": "GUILD_MEMBER_REMOVE"})
        await platform.handle({**event, "s": 22})
        await platform.handle(event)
        assert all(record.conversation == CHANNEL for record in received)
        assert len({record.event_id for record in received}) == 3
        assert received[0].event_id == received[-1].event_id


async def test_launcher_stays_up_across_resume_and_exits_after_terminal_close(connected, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from belle_agent import launcher
    from belle_agent.host import ConversationHost

    gateway = FakeGateway(_opens(ready()), _opens({"op": 0, "s": 2, "t": "RESUMED", "d": {}}))
    platform, _ = await connected(gateway)

    class Host:
        wait_down = ConversationHost.wait_down

        def __init__(self):
            self.platforms = {"discord": platform}
            self._hosted = {"discord": SimpleNamespace(platform=platform)}

        async def start(self):
            pass

        async def stop(self):
            await platform.stop()

    monkeypatch.setattr(launcher, "host_from_config", lambda *args, **kwargs: Host())
    monkeypatch.setattr(launcher, "create_backend", lambda *args: None)
    monkeypatch.setattr(launcher, "second_brain_backend", lambda *args: None)
    # Keep this test from replacing pytest's process signal handlers.
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *args: None)
    serving = asyncio.create_task(launcher.serve({"backend": "test"}, tmp_path))
    gateway.sockets[0].end(1006)
    for _ in range(100):
        await asyncio.sleep(0)
        if len(gateway.sockets) == 2:
            break
    assert len(gateway.sockets) == 2 and not platform._down.is_set() and not serving.done()
    gateway.sockets[1].end(4004)
    assert await asyncio.wait_for(serving, 1) == 1


@pytest.mark.parametrize("kind", ["MESSAGE_REACTION_ADD", "MESSAGE_REACTION_REMOVE"])
@pytest.mark.parametrize("is_bot", [False, True])
async def test_reactions_report_the_actor_bot_flag_from_discord(connected, rest, kind, is_bot):
    platform, received = await connected()
    actor = "1000000000000000999"
    user = {"id": actor, "bot": is_bot}
    data = {"user_id": actor, "channel_id": CHANNEL, "message_id": MESSAGE,
            "guild_id": GUILD, "emoji": {"name": "👍"}}
    if kind == "MESSAGE_REACTION_ADD":
        data["member"] = {"user": user, "roles": []}
    else:
        rest.answer("GET", f"/users/{actor}", (200, user))

    await platform.handle({"op": 0, "s": 11, "t": kind, "d": data})

    assert received[0].actor == actor and received[0].actor_is_bot is is_bot
    assert bool(rest.calls("GET", f"/users/{actor}")) is (kind == "MESSAGE_REACTION_REMOVE")


async def test_a_reaction_with_an_unreadable_actor_is_not_admitted(connected, rest):
    platform, received = await connected()
    rest.answer("GET", f"/users/{ALICE}", (404, {"message": "Unknown User"}))

    outcome = await platform.handle({"op": 0, "s": 11, "t": "MESSAGE_REACTION_REMOVE", "d": {
        "user_id": ALICE, "channel_id": CHANNEL, "message_id": MESSAGE, "guild_id": GUILD,
        "emoji": {"name": "👍"}}})

    assert outcome == "dropped:actor-not-read" and received == []


async def test_blocked_admission_keeps_heartbeats_and_interactions_running(rest):
    waits = asyncio.Queue()

    async def sleep(seconds):
        tick = asyncio.get_running_loop().create_future()
        waits.put_nowait(tick)
        await tick

    gateway = FakeGateway(_opens(ready()))
    client = DiscordClient(TOKEN, transport=rest.transport(), open_socket=gateway.open)
    platform = DiscordPlatform(DiscordSettings(token=TOKEN, typing_indicator=False, pending_events_max=1), client=client)
    platform.gateway._sleep = sleep
    blocked, release, acknowledged, finished, cancelled = (asyncio.Event() for _ in range(5))
    received = []

    async def on_inbound(record):
        received.append(record)
        if len(received) == 1:
            blocked.set()
            try:
                await release.wait()
            finally:
                cancelled.set()
        if len(received) == 2:
            finished.set()
        return "dispatched:test"

    respond = client.respond

    async def response(*args, **kwargs):
        await respond(*args, **kwargs)
        acknowledged.set()

    client.respond = response
    await platform.start(on_inbound)
    await platform.handle(GUILD_CREATE)
    socket = gateway.sockets[0]
    try:
        first_tick = await asyncio.wait_for(waits.get(), 1)
        socket.feed(_message())
        await asyncio.wait_for(blocked.wait(), 1)
        first_tick.set_result(None)
        second_tick = await asyncio.wait_for(waits.get(), 1)
        socket.feed({"op": 0, "s": 10, "t": "MESSAGE_REACTION_ADD", "d": {
            "user_id": ALICE, "channel_id": CHANNEL, "message_id": MESSAGE, "guild_id": GUILD,
            "emoji": {"name": "👍"}, "member": {"user": {"id": ALICE}}}})
        socket.feed({"op": 0, "s": 11, "t": "INTERACTION_CREATE", "d": {
            "id": MESSAGE, "type": 2, "token": "synthetic-interaction", "channel_id": CHANNEL,
            "guild_id": GUILD, "member": {"user": {"id": ALICE}}, "data": {"name": "belle", "type": 1}}})
        socket.feed({"op": 11, "d": None})
        await asyncio.wait_for(acknowledged.wait(), 1)
        assert not release.is_set() and len(received) == 1
        async with asyncio.timeout(1):
            while not platform.gateway._acked:
                await asyncio.sleep(0)
        second_tick.set_result(None)
        await asyncio.wait_for(waits.get(), 1)
        assert socket.closed_with is None and socket.ops().count(1) == 2
        platform.gateway.session_id = "next-session"
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        assert received[1].event_id == "MESSAGE_REACTION_ADD:session-1:10"
    finally:
        await platform.stop()
    assert cancelled.is_set() and socket.closed_with == 1000


async def test_stop_cancels_blocked_event_admission(connected):
    platform, _ = await connected()
    blocked, cancelled = asyncio.Event(), asyncio.Event()

    async def admission(record):
        blocked.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    platform._on_inbound = admission
    platform.gateway._socket.feed(_message())
    await asyncio.wait_for(blocked.wait(), 1)
    await asyncio.wait_for(platform.stop(), 1)
    assert cancelled.is_set() and platform.receiver is None


async def test_event_queue_overflow_reports_terminal_health_and_cancels_admission(connected):
    from belle_agent.platforms.discord.bots import DiscordBots

    platform, _ = await connected(pending_events_max=1)
    bots = DiscordBots([platform])
    bots._serving[platform.bot] = platform
    blocked, cancelled = asyncio.Event(), asyncio.Event()
    received = []

    async def admission(record):
        received.append(record)
        blocked.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    platform._on_inbound = admission
    socket = platform.gateway._socket
    socket.feed(_message())
    await asyncio.wait_for(blocked.wait(), 1)
    assert bots.health == {"": "up"}
    socket.feed(_message(message=str(int(MESSAGE) + 1)))
    socket.feed(_message(message=str(int(MESSAGE) + 2)))

    reason = "discord event queue needs 2 slots, exceeding platforms.discord.pending_events_max=1"
    assert await asyncio.wait_for(platform.wait_down(), 1) == reason
    assert bots.health == {"": reason}
    assert cancelled.is_set() and len(received) == 1
    assert platform.receiver is None and platform.gateway._closed
    assert socket.closed_with == 1000


async def test_stop_cancels_command_registration_before_closing_http(connected, monkeypatch):
    from belle_agent.platforms.discord.commands import Commands

    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def register(self):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert not self._client._http.is_closed
            cancelled.set()

    monkeypatch.setattr(Commands, "register", register)
    platform, _ = await connected()
    await asyncio.wait_for(entered.wait(), 1)
    task = platform._registration
    await asyncio.wait_for(platform.stop(), 1)

    assert cancelled.is_set() and task.cancelled()
    assert platform._registration is None and platform._client._http.is_closed


async def test_an_unexpected_registration_failure_reaches_terminal_health(connected, monkeypatch, caplog):
    from belle_agent.platforms.discord.commands import Commands

    async def register(self):
        raise RuntimeError("synthetic-secret must not be logged")

    monkeypatch.setattr(Commands, "register", register)
    platform, _ = await connected()

    reason = await asyncio.wait_for(platform.wait_down(), 1)
    assert reason == "discord command registration failed (RuntimeError)"
    assert reason in caplog.text and "synthetic-secret" not in caplog.text
    assert platform.receiver is None and platform._registration is None
    assert platform.gateway._closed and platform._client._http.is_closed


async def test_terminal_gateway_failure_cancels_pending_command_registration(connected, monkeypatch):
    from belle_agent.platforms.discord.commands import Commands

    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def register(self):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(Commands, "register", register)
    platform, _ = await connected()
    await asyncio.wait_for(entered.wait(), 1)
    platform.gateway._socket.end(4014)
    await asyncio.wait_for(platform.wait_down(), 1)

    assert cancelled.is_set() and platform._registration is None
