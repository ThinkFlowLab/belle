"""Discord file sends use the post path and verify copied attachments."""

import json

import httpx
import pytest
from discord_transport import TOKEN, FakeRest, Request

from belle_agent.backend import ToolCall
from belle_agent.platforms.discord.client import DiscordClient
from belle_agent.platforms.discord.connector import DiscordPlatform
from belle_agent.platforms.discord.settings import DiscordSettings
from belle_agent.platforms.discord.targets import READ_HISTORY, SEND, VIEW
from belle_agent.tools.host import BoundTurn, ToolHost

GUILD = "1000000000000000200"
CHANNEL = "1000000000000000300"
MESSAGE = "1000000000000000800"
POSTED = "1000000000000000901"
FILE = "1000000000000000900"
NEW_FILE = "1000000000000000902"
TAG = "1000000000000000999"
BOT = "1000000000000000116"
USER = "1000000000000000400"


class FileRest(FakeRest):
    def _answer(self, request):
        if request.method == "POST" and request.url.path.startswith("/api/v10/channels/") and request.url.path.endswith("/threads"):
            self.requests.append(Request(request.method, request.url.path.removeprefix("/api/v10"),
                                         dict(request.headers), request.content))
            return httpx.Response(201, json={"id": POSTED, "type": 11, "parent_id": CHANNEL,
                                             "message": {"id": POSTED, "channel_id": POSTED,
                                                         "attachments": [{"id": NEW_FILE, "filename": "x.txt"}]}})
        if request.method == "POST" and request.url.path.startswith("/api/v10/channels/") and request.url.path.endswith("/messages"):
            destination = request.url.path.split("/")[-2]
            self.requests.append(Request(request.method, request.url.path.removeprefix("/api/v10"),
                                         dict(request.headers), request.content))
            return httpx.Response(200, json={"id": POSTED, "channel_id": destination,
                                             "attachments": [{"id": NEW_FILE, "filename": "note.txt"}]})
        return super()._answer(request)


def setup(*, message_content=True):
    rest = FileRest()
    platform = DiscordPlatform(DiscordSettings(token=TOKEN, bot="east", message_content=message_content), client=DiscordClient(TOKEN, transport=rest.transport()))
    platform.bot_user_id = BOT
    metadata = {"discord_bot": "east", "discord_channel_id": CHANNEL, "discord_user_id": USER,
                "discord_message_id": MESSAGE, "discord_guild_id": GUILD}
    tool = platform.toolkits(lambda: metadata)[3]
    rest.answer("GET", f"/channels/{CHANNEL}", (200, {"id": CHANNEL, "type": 0, "guild_id": GUILD}))
    rest.answer("GET", f"/guilds/{GUILD}", (200, {"id": GUILD, "owner_id": "owner",
                "roles": [{"id": GUILD, "permissions": str(VIEW | READ_HISTORY | SEND)}]}))
    for member in (BOT, USER):
        rest.answer("GET", f"/guilds/{GUILD}/members/{member}", (200, {"user": {"id": member}, "roles": []}))
    rest.answer("GET", f"/guilds/{GUILD}/members", (200, [
        {"user": {"id": member}, "roles": []} for member in (BOT, USER)]))
    return tool, rest, metadata


async def test_inline_file_uses_multipart_and_reports_confirmed_ids():
    tool, rest, _ = setup()
    metadata = {"discord_bot": "east", "discord_channel_id": CHANNEL, "discord_user_id": USER,
                "discord_message_id": MESSAGE, "discord_guild_id": GUILD}
    host = ToolHost([])
    host.serve("discord", tool.platform.toolkits(lambda: metadata), posting_tools=tool.platform.posting_tools)
    turn = BoundTurn("turn", platform="discord", metadata=metadata)
    host.bind("conversation", turn)
    result = json.loads(await host.call(ToolCall("conversation", "send_discord_file", {
        "filename": "note.txt", "content": "hello", "caption": "Here it is",
    })))
    assert result["ok"] and result["message_id"] == POSTED
    assert result["attachment_ids"] == [NEW_FILE] and result["attachment_status"] == "confirmed"
    assert turn.delivered
    assert turn.posted == [{"conversation": CHANNEL, "message_id": POSTED, "thread": ""}]
    raw = rest.requests[-1].body
    assert b"payload_json" in raw and b"files[0]" in raw and b"hello" in raw


async def test_file_tool_schema_and_forum_post_include_applied_tags():
    tool, rest, _ = setup()
    definitions = {item["name"]: item for item in tool.get_tool_definitions()}
    post_definitions = {item["name"]: item for item in tool.post.get_tool_definitions()}
    thread_schema = definitions["send_discord_file"]["input_params"]["properties"]["thread"]
    post_thread_schema = post_definitions["post_message"]["input_params"]["properties"]["thread"]
    assert thread_schema == post_thread_schema
    assert "applied_tag_ids" in thread_schema["properties"]

    rest.answer("GET", f"/channels/{CHANNEL}", (200, {
        "id": CHANNEL, "type": 15, "guild_id": GUILD, "available_tags": [{"id": TAG}], "flags": 0,
    }))
    result = json.loads(await tool.send_discord_file(
        filename="x.txt", content="data", reply_mode="thread",
        thread={"name": "Release", "applied_tag_ids": [TAG]},
    ))

    assert result["ok"] and result["attachment_ids"] == [NEW_FILE]
    body = rest.calls("POST", f"/channels/{CHANNEL}/threads")[0].body
    assert b'"applied_tags": ["' + TAG.encode() + b'"]' in body


async def test_copy_refetches_parent_and_requires_exact_attachment():
    tool, rest, _ = setup()
    source = {"chat_id": CHANNEL, "message_id": MESSAGE, "attachment_id": FILE}
    rest.answer("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}", (200, {"id": MESSAGE, "channel_id": CHANNEL,
                "author": {"id": USER}, "attachments": [{"id": FILE, "filename": "note.txt", "size": 5,
                                                               "url": "https://cdn.discordapp.com/attachments/note.txt"}]}))
    rest.answer("GET", "/attachments/note.txt", (200, b"hello"))
    result = json.loads(await tool.send_discord_file(source_attachment=source))
    assert result["attachment_ids"] == [NEW_FILE]
    assert rest.calls("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}")
    assert rest.calls("GET", "/attachments/note.txt")
    assert b"hello" in rest.requests[-1].body


async def test_missing_or_malformed_source_never_posts():
    tool, rest, _ = setup()
    assert json.loads(await tool.send_discord_file(filename="x", content="a", content_base64="Yg=="))["error"] == "file_source_invalid"
    assert json.loads(await tool.send_discord_file(source_attachment={"chat_id": CHANNEL,
                     "message_id": MESSAGE, "attachment_id": "bad"}))["error"] == "source_attachment_invalid"
    assert not any(getattr(item, "body", b"") and isinstance(item.body, bytes) and b"payload_json" in item.body
                   for item in rest.requests)


async def test_copy_refuses_attachment_not_on_verified_message():
    tool, rest, _ = setup()
    rest.answer("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}", (200, {"id": MESSAGE, "channel_id": CHANNEL,
                "author": {"id": USER}, "attachments": []}))
    result = json.loads(await tool.send_discord_file(source_attachment={"chat_id": CHANNEL,
                             "message_id": MESSAGE, "attachment_id": FILE}))
    assert result["error"] == "attachment_not_in_message"
    assert not any(isinstance(getattr(item, "body", None), bytes) for item in rest.requests)


@pytest.mark.parametrize("widening", [False, True])
async def test_scope_ask_allows_approved_cross_channel_file(widening):
    tool, rest, metadata = setup()
    other, new = "1000000000000000301", "1000000000000000401"
    for channel in (CHANNEL, other):
        overwrites = [{"id": new, "type": 1, "deny": str(VIEW), "allow": "0"}] if widening and channel == CHANNEL else []
        rest.answer("GET", f"/channels/{channel}", (200, {"id": channel, "type": 0,
                    "guild_id": GUILD, "permission_overwrites": overwrites}))
    rest.answer("GET", f"/guilds/{GUILD}/members", (200, [
        {"user": {"id": user}, "roles": []} for user in (BOT, USER, new)]))
    host = ToolHost([])
    host.serve("discord", tool.platform.toolkits(lambda: metadata))
    turn = BoundTurn("turn", platform="discord", metadata=metadata, tools={"send_discord_file": "ask"})
    host.bind("conversation", turn)
    call = ToolCall("conversation", "send_discord_file", {
        "filename": "note.txt", "content": "hello", "chat_id": other}, "call-file")
    assert (await host.authorize(call)).action == "ask"
    assert not rest.calls("POST", f"/channels/{other}/messages")
    result = json.loads(await host.run_confirmed(call, turn))
    assert result["ok"] and result["chat_id"] == other
    assert len(rest.calls("POST", f"/channels/{other}/messages")) == 1


@pytest.mark.parametrize("author, mentions", [(BOT, []), (USER, [{"id": BOT}])])
async def test_copy_uses_returned_attachment_without_message_content_intent(author, mentions):
    tool, rest, _ = setup(message_content=False)
    rest.answer("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}", (200, {
        "id": MESSAGE, "channel_id": CHANNEL, "author": {"id": author}, "mentions": mentions,
        "attachments": [{"id": FILE, "filename": "note.txt", "size": 5,
                         "url": "https://cdn.discordapp.com/attachments/note.txt"}]}))
    rest.answer("GET", "/attachments/note.txt", (200, b"hello"))
    result = json.loads(await tool.send_discord_file(source_attachment={
        "chat_id": CHANNEL, "message_id": MESSAGE, "attachment_id": FILE}))
    assert result["ok"] and result["attachment_ids"] == [NEW_FILE]
    assert len(rest.calls("GET", "/attachments/note.txt")) == 1
    assert len(rest.calls("POST", f"/channels/{CHANNEL}/messages")) == 1


@pytest.mark.parametrize("fields, error", [
    ({}, "content_unavailable"),
    ({"attachments": []}, "content_unavailable"),
    ({"content": "returned", "attachments": []}, "attachment_not_in_message"),
])
async def test_copy_without_intent_refuses_missing_or_withheld_attachment(fields, error):
    tool, rest, _ = setup(message_content=False)
    rest.answer("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}", (200, {
        "id": MESSAGE, "channel_id": CHANNEL, "author": {"id": USER}, **fields}))
    result = json.loads(await tool.send_discord_file(source_attachment={
        "chat_id": CHANNEL, "message_id": MESSAGE, "attachment_id": FILE}))
    assert result["error"] == error
    assert not rest.calls("GET", "/attachments/note.txt")
    assert not rest.calls("POST", f"/channels/{CHANNEL}/messages")
