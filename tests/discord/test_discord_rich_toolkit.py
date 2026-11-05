"""Rich post payloads at the Discord REST boundary."""

import json

import httpx
import pytest
from discord_transport import BOT_ID, TOKEN, FakeRest

from belle_agent.platforms.discord.client import DiscordClient
from belle_agent.platforms.discord.connector import DiscordPlatform
from belle_agent.platforms.discord.settings import DiscordSettings

CHANNEL = "1000000000000000300"
MESSAGE = "1000000000000000800"
GUILD = "1000000000000000200"
ROLE = "1000000000000000500"
REQUESTER = "1000000000000000400"


@pytest.fixture
def rest():
    fake = FakeRest(serve=True)
    fake.channels[CHANNEL] = {"id": CHANNEL, "type": 0, "guild_id": "1000000000000000200"}
    return fake


def toolkit(rest, **settings):
    platform = DiscordPlatform(DiscordSettings(token=TOKEN, **settings), client=DiscordClient(TOKEN, transport=rest.transport()))
    return platform.toolkits(lambda: {"discord_channel_id": CHANNEL})[0]


async def test_embed_only_poll_and_theme_send_without_text(rest):
    tool = toolkit(rest)
    embed = json.loads(await tool.post_message(embeds=[{"title": "A"}]))
    poll = json.loads(await tool.post_message(poll={"question": {"text": "Choose"}, "answers": [
        {"poll_media": {"text": "A"}}, {"poll_media": {"text": "B"}},
    ]}))
    theme = json.loads(await tool.post_message(shared_client_theme={
        "colors": ["5865F2"], "gradient_angle": 0, "base_mix": 58,
    }))

    assert all(result["ok"] for result in (embed, poll, theme))
    bodies = [call.body for call in rest.calls("POST", f"/channels/{CHANNEL}/messages")]
    assert bodies[0]["embeds"] == [{"title": "A"}] and "content" not in bodies[0]
    assert bodies[1]["poll"]["question"]["text"] == "Choose"
    assert bodies[2]["shared_client_theme"]["base_mix"] == 58


async def test_caption_and_link_button_keep_their_distinct_fields(rest):
    tool = toolkit(rest)
    result = json.loads(await tool.post_message("See here", components=[{
        "type": 1, "components": [{"type": 2, "style": 5, "label": "Open", "url": "https://example.org"}],
    }], suppress_notifications=True))

    assert result["ok"]
    body = rest.calls("POST", f"/channels/{CHANNEL}/messages")[0].body
    assert body["content"] == "See here" and body["components"][0]["components"][0]["url"] == "https://example.org"
    assert body["flags"] == 1 << 12


async def test_invalid_rich_payload_refuses_before_any_discord_call(rest):
    result = json.loads(await toolkit(rest).post_message("caption", embeds=[{"title": "x" * 257}]))

    assert result["error"] == "invalid_rich_post" and "embeds[0].title" in result["detail"]
    assert not rest.requests


async def test_split_content_places_embed_once_and_reports_later_failure(rest):
    rest.answer("POST", f"/channels/{CHANNEL}/messages", (200, {"id": MESSAGE, "channel_id": CHANNEL}), (403, {"code": 50013}))
    result = json.loads(await toolkit(rest).post_message("word " * 500, embeds=[{"title": "Once"}]))

    assert result["ok"] is False and result["parts"] == 1
    bodies = [call.body for call in rest.calls("POST", f"/channels/{CHANNEL}/messages")]
    assert bodies[0]["embeds"] == [{"title": "Once"}] and "embeds" not in bodies[1]


async def test_privileged_mentions_require_operator_consent_before_send(rest):
    result = json.loads(await toolkit(rest).post_message("@everyone", mentions={"everyone": True}))

    assert result["error"] == "invalid_rich_post" and "mentions.everyone" in result["detail"]
    assert not rest.calls("POST", f"/channels/{CHANNEL}/messages")


@pytest.mark.parametrize("mentionable, permitted", [(True, True), (False, False)])
async def test_role_ping_requires_mentionable_role_or_bot_permission(rest, mentionable, permitted):
    rest.answer("GET", f"/guilds/{GUILD}", (200, {
        "id": GUILD, "owner_id": "1000000000000000999", "roles": [
            {"id": GUILD, "permissions": str((1 << 10) | (1 << 11))},
            {"id": ROLE, "permissions": "0", "mentionable": mentionable},
        ],
    }))
    rest.answer("GET", f"/guilds/{GUILD}/members", (200, [
        {"user": {"id": user}, "roles": []} for user in (BOT_ID, REQUESTER)
    ]))
    tool = toolkit(rest, allow_role_mentions=True)
    tool._platform.bot_user_id = BOT_ID
    tool._metadata_provider = lambda: {"discord_channel_id": CHANNEL, "discord_user_id": REQUESTER}

    result = json.loads(await tool.post_message(f"Hello <@&{ROLE}>", mentions={"roles": [ROLE]}))

    assert result["ok"] is permitted
    if permitted:
        assert rest.calls("POST", f"/channels/{CHANNEL}/messages")[0].body["allowed_mentions"] == {
            "parse": [], "roles": [ROLE],
        }
    else:
        assert "mentions.roles" in result["detail"]
        assert not rest.calls("POST", f"/channels/{CHANNEL}/messages")


async def test_inline_upload_uses_multipart_with_payload_json():
    posts = []

    def answer(request):
        if request.method == "GET":
            return httpx.Response(200, json={"id": CHANNEL, "type": 0})
        posts.append(request)
        return httpx.Response(200, json={
            "id": MESSAGE, "channel_id": CHANNEL,
            "attachments": [{"id": "1000000000000000900", "filename": "note.txt"}],
        })

    client = DiscordClient(TOKEN, transport=httpx.MockTransport(answer))
    platform = DiscordPlatform(DiscordSettings(token=TOKEN), client=client)
    tool = platform.toolkits(lambda: {"discord_channel_id": CHANNEL})[0]
    result = json.loads(await tool.post_message("caption", attachments=[{"filename": "note.txt", "content": "hello"}]))

    assert result["ok"]
    assert result["attachment_status"] == "confirmed"
    assert result["attachment_ids"] == ["1000000000000000900"]
    assert "multipart/form-data" in posts[0].headers["content-type"]
    assert b'"filename": "note.txt"' in posts[0].content
    assert b"hello" in posts[0].content
