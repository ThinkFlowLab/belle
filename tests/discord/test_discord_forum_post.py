"""Forum and media post creation uses the channel-specific request shape."""

import json
from types import SimpleNamespace

import pytest
from discord_transport import BOT_ID, TOKEN, FakeRest

from belle_agent.platforms.discord.client import DiscordClient, DiscordError
from belle_agent.platforms.discord.forum_post import create_forum_thread, forum_request_kwargs, forum_thread_payload
from belle_agent.platforms.discord.targets import TargetResolver

TAG = "1000000000000000999"
CHANNEL = {"id": "1000000000000000900", "type": 15, "available_tags": [{"id": TAG}], "flags": 0}


@pytest.mark.parametrize("kind", [15, 16])
def test_forum_and_media_thread_payload(kind):
    channel = {**CHANNEL, "type": kind}
    result = forum_thread_payload(
        channel, {"name": "Release notes", "applied_tag_ids": [TAG], "auto_archive_minutes": 1440,
                  "rate_limit_per_user": 15},
        {"content": "Shipping", "allowed_mentions": {"parse": []}},
    )
    assert result == {
        "name": "Release notes", "applied_tags": [TAG], "auto_archive_duration": 1440,
        "rate_limit_per_user": 15, "message": {"content": "Shipping", "allowed_mentions": {"parse": []}},
    }


@pytest.mark.parametrize(("options", "message", "field"), [
    (None, {"content": "x"}, "thread"),
    ({}, {"content": "x"}, "thread.name"),
    ({"name": "  "}, {"content": "x"}, "thread.name"),
    ({"name": "x", "applied_tag_ids": ["1000000000000000000"]}, {"content": "x"}, "thread.applied_tag_ids[0]"),
    ({"name": "x", "applied_tag_ids": [TAG, TAG]}, {"content": "x"}, "thread.applied_tag_ids"),
    ({"name": "x", "auto_archive_minutes": 61}, {"content": "x"}, "thread.auto_archive_minutes"),
    ({"name": "x", "rate_limit_per_user": True}, {"content": "x"}, "thread.rate_limit_per_user"),
    ({"name": "x"}, {"poll": {"question": "?"}}, "message.poll"),
    ({"name": "x"}, {"flags": 32768, "components": [{"type": 10}]}, "message.flags"),
    ({"name": "x"}, {}, "message"),
])
def test_bad_forum_shape_names_field(options, message, field):
    with pytest.raises(DiscordError, match=field.replace("[", r"\[").replace("]", r"\]")):
        forum_thread_payload(CHANNEL, options, message)


def test_channel_required_tag_and_wrong_type():
    with pytest.raises(DiscordError, match="thread.applied_tag_ids"):
        forum_thread_payload({**CHANNEL, "flags": 1 << 4}, {"name": "x"}, {"content": "x"})
    with pytest.raises(DiscordError, match="chat_id"):
        forum_thread_payload({**CHANNEL, "type": 0}, {"name": "x"}, {"content": "x"})


def test_file_only_start_is_sendable():
    assert forum_thread_payload(CHANNEL, {"name": "file"}, {"attachments": [{"id": 0}]}, has_files=True)


def test_file_post_nests_payload_json_and_preserves_upload():
    files = (("files[0]", ("note.txt", b"hello")),)
    request = forum_request_kwargs(
        CHANNEL, {"name": "file"}, {"content": "Caption", "attachments": [{"id": 0, "filename": "note.txt"}]},
        files,
    )
    assert request["files"] == list(files)
    assert json.loads(request["data"]["payload_json"])["message"]["attachments"][0]["filename"] == "note.txt"


async def test_forum_create_uses_threads_endpoint_and_nested_first_message():
    thread = "1000000000000000911"
    first = "1000000000000000912"
    rest = FakeRest()
    path = f"/channels/{CHANNEL['id']}/threads"
    rest.answer("POST", path, (201, {
        "id": thread, "type": 11, "parent_id": CHANNEL["id"],
        "message": {"id": first, "channel_id": thread},
    }))
    client = DiscordClient(TOKEN, transport=rest.transport())
    assert await create_forum_thread(client, CHANNEL, {"name": "News"}, {"content": "Hello"}) == (
        thread, first, {"id": first, "channel_id": thread},
    )
    assert rest.calls("POST", path)[0].body == {"name": "News", "message": {"content": "Hello"}}
    assert not rest.calls("POST", f"/channels/{CHANNEL['id']}/messages")


@pytest.mark.parametrize(("status", "response", "code"), [
    (403, {"code": 50013}, "forbidden"),
    (201, {"id": "1000000000000000911", "type": 11, "parent_id": CHANNEL["id"]}, "delivery_uncertain_do_not_retry"),
])
async def test_forum_create_refuses_denial_or_unconfirmed_response(status, response, code):
    rest = FakeRest()
    rest.answer("POST", f"/channels/{CHANNEL['id']}/threads", (status, response))
    with pytest.raises(DiscordError, match=code):
        await create_forum_thread(DiscordClient(TOKEN, transport=rest.transport()), CHANNEL,
                                  {"name": "News"}, {"content": "Hello"})


@pytest.mark.parametrize("kind", [15, 16])
@pytest.mark.parametrize("can_send", [True, False])
async def test_forum_target_requires_bot_send_permission(kind, can_send):
    guild = "1000000000000000888"
    requester = "1000000000000000777"
    rest = FakeRest()
    rest.answer("GET", f"/channels/{CHANNEL['id']}", (200, {
        **CHANNEL, "type": kind, "guild_id": guild, "permission_overwrites": [],
    }))
    rest.answer("GET", f"/guilds/{guild}", (200, {
        "id": guild, "owner_id": "1000000000000000666",
        "roles": [{"id": guild, "permissions": str((1 << 10) | (1 << 11))}],
    }))
    members = [
        {"user": {"id": requester}, "roles": []},
        {"user": {"id": BOT_ID}, "roles": []},
    ]
    if not can_send:
        rest.answer("GET", f"/channels/{CHANNEL['id']}", (200, {
            **CHANNEL, "type": kind, "guild_id": guild,
            "permission_overwrites": [{"id": BOT_ID, "type": 1, "allow": "0", "deny": str(1 << 11)}],
        }))
    rest.answer("GET", f"/guilds/{guild}/members", (200, members))
    platform = SimpleNamespace(_client=DiscordClient(TOKEN, transport=rest.transport()), bot_user_id=BOT_ID)
    if can_send:
        assert requester in (await TargetResolver(platform).resolve(CHANNEL["id"], requester)).readers
    else:
        with pytest.raises(DiscordError, match="bot_cannot_post_target"):
            await TargetResolver(platform).resolve(CHANNEL["id"], requester)
