"""Discord's current place evidence gates session results and delivery."""

from types import SimpleNamespace

import pytest
from discord_transport import BOT_ID, TOKEN, FakeRest

from belle_agent.platforms.discord.client import DiscordClient
from belle_agent.platforms.discord.connector import DiscordPlatform
from belle_agent.platforms.discord.settings import DiscordSettings
from belle_agent.scheduling.address import Address
from belle_agent.scheduling.refusals import Refused
from belle_agent.scheduling.session_toolkit import _COMMAND, SessionToolkit
from belle_agent.scheduling.settings import SchedulingSettings
from belle_agent.scheduling.toolkit import Caller, ScheduleToolkit

DM = "1000000000000000201"
USER = "1000000000000000202"
GUILD_ONE = "1000000000000000301"
GUILD_TWO = "1000000000000000302"
CHANNEL_ONE = "1000000000000000401"
CHANNEL_TWO = "1000000000000000402"


def platform_for(rest: FakeRest) -> DiscordPlatform:
    platform = DiscordPlatform(
        DiscordSettings(token=TOKEN, bot="east"),
        client=DiscordClient(TOKEN, transport=rest.transport()),
    )
    platform.bot_user_id = BOT_ID
    return platform


async def test_session_result_is_visible_in_the_requesters_current_dm():
    rest = FakeRest()
    rest.answer("GET", f"/channels/{DM}", (200, {
        "id": DM, "type": 1, "recipients": [{"id": USER}],
    }))
    platform = platform_for(rest)
    caller = Caller(Address("discord", "east:direct", DM, ""), USER, "dm")
    entry = {
        "origin": caller.place.json(), "requested_by_user_id": USER,
    }
    command = _COMMAND.set((caller, "command-id", True))
    try:
        toolkit = SessionToolkit(None, None, SimpleNamespace(_places=lambda name: platform), None)
        assert await toolkit._result_visible(entry, caller)
        assert not await platform.can_read("1000000000000000299", DM, workspace="east:direct")
    finally:
        _COMMAND.reset(command)


async def test_discord_place_checks_reject_a_mismatched_workspace():
    rest = FakeRest()
    rest.answer("GET", f"/channels/{DM}", (200, {
        "id": DM, "type": 1, "recipients": [{"id": USER}],
    }))
    platform = platform_for(rest)

    checked = await platform.check_place(DM, "", workspace="west:direct")

    assert not checked.exists
    assert not await platform.can_read(USER, DM, workspace="west:direct")


async def test_discord_audience_comparison_refuses_a_different_guild():
    rest = FakeRest()
    for channel, guild in ((CHANNEL_ONE, GUILD_ONE), (CHANNEL_TWO, GUILD_TWO)):
        rest.answer("GET", f"/channels/{channel}", (200, {
            "id": channel, "type": 0, "guild_id": guild, "permission_overwrites": [],
        }))
        rest.answer("GET", f"/guilds/{guild}", (200, {
            "id": guild, "owner_id": "1000000000000000998",
            "roles": [{"id": guild, "permissions": str(1 << 10)}],
        }))
        rest.answer("GET", f"/guilds/{guild}/members", (200, [{
            "user": {"id": BOT_ID}, "roles": [],
        }]))
    platform = platform_for(rest)

    assert await platform.newcomers(CHANNEL_ONE, CHANNEL_TWO) is None


async def test_place_check_denies_a_guild_channel_without_send_permission():
    rest = FakeRest()
    rest.answer("GET", f"/channels/{CHANNEL_ONE}", (200, {
        "id": CHANNEL_ONE, "type": 0, "guild_id": GUILD_ONE, "permission_overwrites": [],
    }))
    rest.answer("GET", f"/guilds/{GUILD_ONE}", (200, {
        "id": GUILD_ONE, "owner_id": "1000000000000000998",
        "roles": [{"id": GUILD_ONE, "permissions": str(1 << 10)}],
    }))
    rest.answer("GET", f"/guilds/{GUILD_ONE}/members", (200, [{
        "user": {"id": BOT_ID}, "roles": [],
    }]))
    platform = platform_for(rest)

    checked = await platform.check_place(CHANNEL_ONE, "", workspace=f"east:{GUILD_ONE}")

    assert checked.exists
    assert not checked.member
    assert "permission to post" in checked.reason


async def test_creator_dm_refuses_a_guild_workspace_without_opening_a_dm():
    rest = FakeRest()
    platform = platform_for(rest)
    host = SimpleNamespace(platforms={"discord": object()}, places=lambda name: platform)
    toolkit = ScheduleToolkit(None, None, SchedulingSettings(), host, None)
    origin = Address("discord", f"east:{GUILD_ONE}", CHANNEL_ONE, "")

    with pytest.raises(Refused) as refusal:
        await toolkit._target("creator_dm", origin, USER)

    assert refusal.value.code == "delivery_unavailable"
    assert "east:direct" in refusal.value.detail
    assert not rest.calls("POST", "/users/@me/channels")


@pytest.mark.parametrize("kind", [11, 12])
async def test_a_private_thread_can_validate_its_own_session_address(kind):
    rest = FakeRest()
    rest.answer("GET", f"/channels/{CHANNEL_ONE}", (200, {
        "id": CHANNEL_ONE, "type": 0, "guild_id": GUILD_ONE,
    }))
    rest.answer("GET", f"/channels/{CHANNEL_TWO}", (200, {
        "id": CHANNEL_TWO, "type": kind, "guild_id": GUILD_ONE, "parent_id": CHANNEL_ONE,
    }))
    rest.answer("GET", f"/channels/{CHANNEL_TWO}/thread-members", (200, []))
    rest.answer("GET", f"/guilds/{GUILD_ONE}", (200, {
        "id": GUILD_ONE, "owner_id": BOT_ID,
        "roles": [{"id": GUILD_ONE, "permissions": "0"}],
    }))
    rest.answer("GET", f"/guilds/{GUILD_ONE}/members", (200, [{
        "user": {"id": BOT_ID}, "roles": [],
    }]))
    platform = platform_for(rest)

    checked = await platform.check_place(CHANNEL_TWO, CHANNEL_TWO, workspace=f"east:{GUILD_ONE}")

    assert checked.exists is (kind == 12)
    assert checked.member is (kind == 12)
    parent_address = await platform.check_place(CHANNEL_ONE, CHANNEL_TWO, workspace=f"east:{GUILD_ONE}")
    assert parent_address.exists and parent_address.member
