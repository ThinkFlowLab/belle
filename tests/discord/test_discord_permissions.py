"""Timeout restrictions apply to both direct access and audience checks."""

import json

import pytest
from test_discord_reading import BOT, CHANNEL, GUILD, MESSAGE, NEW, PRIVATE, USER

from belle_agent.backend import ToolCall
from belle_agent.platforms.discord.client import DiscordError
from belle_agent.platforms.discord.targets import (
    ADMIN,
    MANAGE_THREADS,
    READ_HISTORY,
    SEND,
    VIEW,
    TargetResolver,
    permissions,
)
from belle_agent.tools.host import BoundTurn, ToolHost

ROLE = "1000000000000000505"
FUTURE = "2999-01-01T00:00:00+00:00"
PAST = "2000-01-01T00:00:00Z"


def _guild(*, owner="owner", extra=MANAGE_THREADS):
    return {"id": GUILD, "owner_id": owner, "roles": [
        {"id": GUILD, "permissions": str(VIEW | READ_HISTORY)},
        {"id": ROLE, "permissions": str(extra)},
    ]}


@pytest.mark.parametrize("until", [None, PAST, FUTURE])
@pytest.mark.parametrize("denied", [0, VIEW, READ_HISTORY])
def test_timeout_mask_follows_overwrites_and_preserves_only_existing_read_permissions(until, denied):
    channel = {"permission_overwrites": [
        {"id": USER, "type": 1, "allow": str(SEND | MANAGE_THREADS), "deny": str(denied)},
    ]}
    member = {"user": {"id": USER}, "roles": [ROLE], "communication_disabled_until": until}
    expected = (VIEW | READ_HISTORY | SEND | MANAGE_THREADS) & ~denied
    if until == FUTURE:
        expected &= VIEW | READ_HISTORY
    assert permissions(_guild(), channel, member) == expected


@pytest.mark.parametrize("owner,extra", [(USER, MANAGE_THREADS), ("owner", ADMIN)])
def test_owner_and_administrator_are_exempt_from_timeout(owner, extra):
    member = {"user": {"id": USER}, "roles": [ROLE], "communication_disabled_until": FUTURE}
    channel = {"permission_overwrites": [{"id": USER, "type": 1, "allow": "0", "deny": str(VIEW)}]}
    assert permissions(_guild(owner=owner, extra=extra), channel, member) == -1


@pytest.mark.parametrize("until", [False, 0, [], {}, "", "invalid", "2026-10-24", "2026-10-24T00:00:00"])
def test_malformed_timeout_refuses_permission_evidence(until):
    member = {"user": {"id": USER}, "roles": [ROLE], "communication_disabled_until": until}
    with pytest.raises(ValueError, match="communication_disabled_until must be null or an ISO8601 timestamp with timezone"):
        permissions(_guild(), {}, member)


async def test_timed_out_manager_cannot_read_unjoined_private_thread_from_dm(read_setup):
    tool, rest, metadata = read_setup
    metadata.update(discord_channel_type="dm", discord_guild_id="")
    rest.answer("GET", f"/channels/{CHANNEL}", (200, {"id": CHANNEL, "type": 1, "recipients": [{"id": USER}]}))
    parent = "1000000000000000506"
    rest.answer("GET", f"/channels/{parent}", (200, {"id": parent, "type": 0, "guild_id": GUILD}))
    rest.answer("GET", f"/guilds/{GUILD}", (200, _guild()))
    rest.answer("GET", f"/guilds/{GUILD}/members/{USER}", (200, {
        "user": {"id": USER}, "roles": [ROLE], "communication_disabled_until": FUTURE,
    }))
    rest.answer("GET", f"/channels/{PRIVATE}", (200, {
        "id": PRIVATE, "type": 12, "guild_id": GUILD, "parent_id": parent,
    }))
    rest.answer("GET", f"/channels/{PRIVATE}/thread-members/{USER}", (404, {}))

    result = json.loads(await tool.read_discord_conversation(chat_id=PRIVATE, message_id=MESSAGE))

    assert result["error"] == "requester_cannot_read_target"
    assert rest.calls("GET", f"/channels/{PRIVATE}/thread-members/{USER}")
    assert not rest.calls("GET", f"/channels/{PRIVATE}/messages/{MESSAGE}")


async def test_source_audience_includes_timed_out_manager_who_cannot_read_private_target(read_setup):
    tool, rest, metadata = read_setup
    rest.answer("GET", f"/guilds/{GUILD}", (200, _guild()))
    rest.answer("GET", f"/guilds/{GUILD}/members", (200, [
        {"user": {"id": user}, "roles": []} for user in (BOT, USER)
    ] + [{"user": {"id": NEW}, "roles": [ROLE], "communication_disabled_until": FUTURE}]))
    rest.answer("GET", f"/channels/{PRIVATE}", (200, {
        "id": PRIVATE, "type": 12, "guild_id": GUILD, "parent_id": CHANNEL,
    }))
    for user in (BOT, USER):
        rest.answer("GET", f"/channels/{PRIVATE}/thread-members/{user}", (200, {"user_id": user}))
    rest.answer("GET", f"/channels/{PRIVATE}/thread-members", (200, [{"user_id": user} for user in (BOT, USER)]))
    resolver = TargetResolver(tool.platform)
    assert NEW in (await resolver.resolve(CHANNEL, USER, posting=False)).readers
    assert NEW not in (await resolver.resolve(PRIVATE, USER, posting=False)).readers
    host = ToolHost([])
    host.serve("discord", tool.platform.toolkits(lambda: metadata))
    host.bind("conversation", BoundTurn("turn", platform="discord", metadata=metadata))

    result = json.loads(await host.call(ToolCall("conversation", "read_discord_conversation", {
        "chat_id": PRIVATE, "message_id": MESSAGE,
    })))

    assert result["error"] == "read_would_disclose_to_other_readers"
    assert not rest.calls("GET", f"/channels/{PRIVATE}/messages/{MESSAGE}")


async def test_malformed_timeout_refuses_direct_read_and_audience_resolution(read_setup):
    tool, rest, _ = read_setup
    member = {"user": {"id": USER}, "roles": [], "communication_disabled_until": "invalid"}
    rest.answer("GET", f"/guilds/{GUILD}/members/{USER}", (200, member))
    result = json.loads(await tool.read_discord_conversation(message_id=MESSAGE))
    assert result["error"] == "bad_response"
    assert not rest.calls("GET", f"/channels/{CHANNEL}/messages/{MESSAGE}")
    rest.answer("GET", f"/guilds/{GUILD}/members", (200, [member]))
    with pytest.raises(DiscordError, match="bad_response"):
        await TargetResolver(tool.platform).resolve(CHANNEL, USER, posting=False)


async def test_audience_keeps_current_viewers_separate_from_history_readers(read_setup):
    tool, rest, _ = read_setup
    rest.answer("GET", f"/channels/{CHANNEL}", (200, {
        "id": CHANNEL, "type": 0, "guild_id": GUILD, "permission_overwrites": [
            {"id": NEW, "type": 1, "deny": str(READ_HISTORY), "allow": "0"}]}))
    rest.answer("GET", f"/guilds/{GUILD}/members", (200, [
        {"user": {"id": user}, "roles": []} for user in (BOT, USER, NEW)]))

    target = await TargetResolver(tool.platform).resolve(CHANNEL, USER, posting=False)

    assert target.readers == frozenset({BOT, USER, NEW})
    assert target.history_readers == frozenset({BOT, USER})
