"""Mattermost place checks for unattended delivery."""

from test_mattermost_tools import ALICE, BOB, ELSEWHERE, LOUNGE, OUTSIDE, PAIR, QUIET, TEAM
from transport import BOT_USER

from belle_agent.platforms.base import PlaceCheck


async def test_place_checks_use_live_channel_and_membership(turn):
    platform, rest = turn.connector, turn.rest
    assert platform.workspaces is None
    assert await platform.check_place(QUIET, "", workspace=TEAM) == PlaceCheck(True, True, chat_type="P")
    assert (await platform.check_place(LOUNGE, "", workspace=TEAM)).chat_type == "O"
    assert not (await platform.check_place(QUIET, "", workspace="other")).exists
    assert (await platform.check_place(OUTSIDE, "", workspace=TEAM)).exists
    assert not (await platform.check_place(OUTSIDE, "", workspace=TEAM)).member
    assert not (await platform.check_place("missing", "", workspace=TEAM)).exists

    root = "r" * 26
    rest.post(root, QUIET, ALICE)
    assert await platform.check_place(QUIET, root, workspace=TEAM) == PlaceCheck(True, True, chat_type="P")
    assert not (await platform.check_place(PAIR, root, workspace=TEAM)).exists

    rest.members[QUIET].remove(BOT_USER)
    assert not (await platform.check_place(QUIET, "", workspace=TEAM)).member


async def test_audience_and_direct_message_keep_the_right_workspace(turn):
    platform = turn.connector
    assert await platform.can_read(BOB, LOUNGE, workspace=TEAM)
    assert not await platform.can_read("visitor", LOUNGE, workspace=TEAM)
    assert await platform.can_read(BOB, QUIET, workspace=TEAM)
    assert not await platform.can_read(BOB, QUIET, workspace="other")
    assert await platform.newcomers(PAIR, QUIET, workspace=TEAM) == (BOB,)
    assert await platform.newcomers(PAIR, LOUNGE, workspace=TEAM) is None
    assert platform.workspace("D", "") == "direct"
    dm = await platform.open_direct_message(BOB, workspace=TEAM)
    assert await platform.check_place(dm, "", workspace="direct") == PlaceCheck(True, True, chat_type="D")
    assert platform.mention(BOB) == BOB
    platform.directory.learn_name(BOB, "bob")
    assert platform.mention(BOB) == "@bob"


async def test_another_team_is_not_accepted_under_the_origin_team(turn):
    platform = turn.connector
    assert not (await platform.check_place(ELSEWHERE, "", workspace=TEAM)).exists
