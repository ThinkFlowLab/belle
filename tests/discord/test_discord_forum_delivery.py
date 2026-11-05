"""Forum and media destinations create a thread with an initial message."""

import json

import httpx
import pytest
from discord_transport import TOKEN, FakeRest

from belle_agent.platforms.discord.client import DiscordClient
from belle_agent.platforms.discord.connector import DiscordPlatform
from belle_agent.platforms.discord.settings import DiscordSettings
from belle_agent.tools.host import ToolHost
from belle_agent.tools.server import PATH, ToolServer

FORUM = "1000000000000000300"
GUILD = "1000000000000000400"
THREAD = "1000000000000000800"
FIRST = "1000000000000000901"
SECOND = "1000000000000000902"
TAG = "1000000000000000999"


def toolkit(rest, *, kind=15, guild_id=""):
    platform = DiscordPlatform(DiscordSettings(token=TOKEN), client=DiscordClient(TOKEN, transport=rest.transport()))
    tool = platform.toolkits(lambda: {"discord_channel_id": FORUM, "discord_guild_id": guild_id})[0]
    rest.answer("GET", f"/channels/{FORUM}", (200, {
        "id": FORUM, "type": kind, "available_tags": [{"id": TAG}], "flags": 0,
    }))
    return tool


async def test_mcp_tool_catalog_describes_forum_and_media_options():
    tool = toolkit(FakeRest())

    async def conversation_of(meta, headers):
        return ""

    server = ToolServer(ToolHost([tool]), conversation_of)
    await server.start()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{server.port}") as client:
            response = await client.post(PATH, headers={"Authorization": f"Bearer {server.token}"}, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
            })
        card = next(item for item in response.json()["result"]["tools"] if item["name"] == "post_message")
    finally:
        await server.close()

    properties = card["inputSchema"]["properties"]
    thread = properties["thread"]
    tags = thread["properties"]["applied_tag_ids"]

    assert "forum" in card["description"] and "media" in card["description"]
    assert "thread.name" in properties["reply_mode"]["description"]
    assert "Required for a new forum or media post" in thread["properties"]["name"]["description"]
    assert tags["maxItems"] == 5 and tags["uniqueItems"] is True
    assert tags["items"]["pattern"] == "^[0-9]{17,20}$"


@pytest.mark.parametrize("kind", [15, 16])
async def test_post_creates_forum_or_media_thread_and_reports_every_part(kind):
    rest = FakeRest()
    rest.answer("POST", f"/channels/{FORUM}/threads", (201, {
        "id": THREAD, "type": 11, "parent_id": FORUM,
        "message": {"id": FIRST, "channel_id": THREAD},
    }))
    rest.answer("POST", f"/channels/{THREAD}/messages", (200, {"id": SECOND, "channel_id": THREAD}))
    tool = toolkit(rest, kind=kind, guild_id=GUILD)
    tool._platform._learn_channel({"id": FORUM, "type": kind})

    result = json.loads(await tool.post_message(
        "word " * 500, reply_mode="thread", thread={"name": "News", "applied_tag_ids": [TAG]},
    ))

    assert result["ok"] is True and result["chat_id"] == FORUM and result["thread_id"] == THREAD
    assert result["url"] == f"https://discord.com/channels/{GUILD}/{THREAD}/{FIRST}"
    assert result["part_message_ids"] == [FIRST, SECOND]
    assert result["part_placements"] == [
        {"chat_id": FORUM, "thread_id": THREAD, "message_id": FIRST},
        {"chat_id": FORUM, "thread_id": THREAD, "message_id": SECOND},
    ]
    first = rest.calls("POST", f"/channels/{FORUM}/threads")[0].body
    assert first["name"] == "News" and first["applied_tags"] == [TAG]
    assert rest.calls("GET", f"/channels/{FORUM}")
    assert "content" in first["message"]
    assert not rest.calls("POST", f"/channels/{FORUM}/messages")


@pytest.mark.parametrize(("thread", "error"), [
    ({}, "thread.name"),
    ({"name": "   "}, "thread.name"),
    ({"name": "News", "applied_tag_ids": ["1000000000000000777"]}, "thread.applied_tag_ids[0]"),
])
async def test_invalid_forum_options_refuse_before_post(thread, error):
    rest = FakeRest()
    result = json.loads(await toolkit(rest).post_message("x", reply_mode="thread", thread=thread))
    assert result["ok"] is False and error in result["error"] + result.get("detail", "")
    assert not rest.calls("POST", f"/channels/{FORUM}/threads")


async def test_forum_permission_denial_has_no_confirmed_parts():
    rest = FakeRest()
    rest.answer("POST", f"/channels/{FORUM}/threads", (403, {"code": 50013}))
    result = json.loads(await toolkit(rest).post_message("x", reply_mode="thread", thread={"name": "News"}))
    assert result["error"] == "forbidden" and "parts" not in result


async def test_later_part_failure_reports_thread_and_first_message():
    rest = FakeRest()
    rest.answer("POST", f"/channels/{FORUM}/threads", (201, {
        "id": THREAD, "type": 11, "parent_id": FORUM,
        "message": {"id": FIRST, "channel_id": THREAD},
    }))
    rest.answer("POST", f"/channels/{THREAD}/messages", (403, {"code": 50013}))
    result = json.loads(await toolkit(rest).post_message("word " * 500, reply_mode="thread",
                                                        thread={"name": "News"}))
    assert result["ok"] is False and result["error"] == "forbidden"
    assert result["part_placements"] == [{"chat_id": FORUM, "thread_id": THREAD, "message_id": FIRST}]


async def test_forum_upload_uses_multipart_with_nested_message():
    requests = []

    def answer(request):
        if request.method == "GET":
            return httpx.Response(200, json={"id": FORUM, "type": 15, "flags": 0})
        requests.append(request)
        return httpx.Response(201, json={
            "id": THREAD, "type": 11, "parent_id": FORUM,
            "message": {"id": FIRST, "channel_id": THREAD},
        })

    client = DiscordClient(TOKEN, transport=httpx.MockTransport(answer))
    platform = DiscordPlatform(DiscordSettings(token=TOKEN), client=client)
    tool = platform.toolkits(lambda: {"discord_channel_id": FORUM})[0]
    result = json.loads(await tool.post_message(
        "Caption", reply_mode="thread", thread={"name": "File"},
        attachments=[{"filename": "note.txt", "content": "hello"}],
    ))
    assert result["ok"] is True and result["thread_id"] == THREAD
    assert "multipart/form-data" in requests[0].headers["content-type"]
    assert b'"message":' in requests[0].content and b"hello" in requests[0].content


@pytest.mark.parametrize("kind", [15, 16])
async def test_platform_renders_plain_text_for_forum_thread(kind):
    rest = FakeRest()
    rest.answer("GET", f"/channels/{FORUM}", (200, {"id": FORUM, "type": kind, "flags": 0}))
    rest.answer("POST", f"/channels/{FORUM}/threads", (201, {
        "id": THREAD, "type": 11, "parent_id": FORUM,
        "message": {"id": FIRST, "channel_id": THREAD},
    }))
    rest.answer("POST", f"/channels/{THREAD}/messages", (200, {"id": SECOND, "channel_id": THREAD}))
    platform = DiscordPlatform(DiscordSettings(token=TOKEN), client=DiscordClient(TOKEN, transport=rest.transport()))

    result = await platform.post_parts(
        FORUM, "", "word " * 500, validate_placement=True, require_thread=True,
        thread_options={"name": "News"},
    )

    assert result == [
        {"chat_id": FORUM, "thread_id": THREAD, "message_id": FIRST},
        {"chat_id": FORUM, "thread_id": THREAD, "message_id": SECOND},
    ]
    first = rest.calls("POST", f"/channels/{FORUM}/threads")[0].body["message"]["content"]
    second = rest.calls("POST", f"/channels/{THREAD}/messages")[0].body["content"]
    assert first.split() + second.split() == ["word"] * 500


@pytest.mark.parametrize("kind", [15, 16])
@pytest.mark.parametrize("body", [None, [], b"not JSON", {}])
async def test_malformed_forum_success_is_uncertain_and_never_retried(kind, body):
    rest = FakeRest()
    rest.answer("POST", f"/channels/{FORUM}/threads", (201, body))

    result = json.loads(await toolkit(rest, kind=kind).post_message(
        "news", reply_mode="thread", thread={"name": "News"},
    ))

    assert result["error"] == "delivery_uncertain_do_not_retry"
    assert len(rest.calls("POST", f"/channels/{FORUM}/threads")) == 1
    assert "part_message_ids" not in result
