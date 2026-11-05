from unittest.mock import AsyncMock

import pytest

from belle_agent.platforms.base import MARK_DONE, MARK_QUEUED, MARK_REACTIONS, MARK_REFUSED, InboundMessage
from belle_agent.platforms.loopback.platform import LoopbackPlatform
from belle_agent.platforms.marks import react_marks, reaction_changes


@pytest.mark.parametrize("state", MARK_REACTIONS)
async def test_transports_apply_the_same_plan_that_loopback_records(state):
    react = AsyncMock()
    inbound = InboundMessage(platform="loopback", workspace="local", conversation="room", message_id="m", sender="u", text="")
    platform = LoopbackPlatform()

    await platform.mark(inbound, state)
    await react_marks(react, "room", "m", state, lambda key: key)

    assert platform.mark_changes == [("m", reaction_changes(state, lambda key: key))]
    assert tuple((call.args[2], call.kwargs["remove"]) for call in react.await_args_list) == platform.mark_changes[0][1]


def test_disabled_reactions_are_omitted_and_removals_precede_additions():
    assert reaction_changes("done", lambda key: {"acknowledgement_emoji": "eyes", "completed_emoji": "check"}[key]) == (
        ("eyes", True), ("check", False),
    )
    assert reaction_changes("done", lambda key: "") == ()
    assert reaction_changes("unknown", lambda key: key) == ()

EMOJI = {"acknowledgement_emoji": "eyes", "queued_emoji": "hourglass", "completed_emoji": "white_check_mark"}


async def _marks(state, emoji=EMOJI):
    calls = []

    async def react(conversation, message_id, name, remove=False):
        calls.append((conversation, message_id, name, remove))

    await react_marks(react, "C1", "M1", state, lambda key: emoji.get(key, ""))
    return calls


async def test_a_mark_takes_off_its_reactions_before_it_adds_its_own():
    assert await _marks(MARK_DONE) == [("C1", "M1", "eyes", True), ("C1", "M1", "white_check_mark", False)]


async def test_each_reaction_a_mark_adds_is_added_in_the_table_s_order():
    assert await _marks(MARK_QUEUED) == [("C1", "M1", "eyes", False), ("C1", "M1", "hourglass", False)]


async def test_a_key_the_settings_name_no_reaction_for_is_skipped():
    assert await _marks(MARK_REFUSED) == []
    assert await _marks(MARK_DONE, {"completed_emoji": "white_check_mark"}) == [("C1", "M1", "white_check_mark", False)]


async def test_a_state_with_no_reactions_changes_nothing():
    assert await _marks("no such state") == []
