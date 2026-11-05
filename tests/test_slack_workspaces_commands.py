"""The slash command and the Home tab welcome with several Slack workspaces in one process.

Each workspace has the command on its own connection. The two workspaces
here use the same channel id, user id and message ids on purpose, as in
``tests/test_slack_workspaces.py``, whose fixtures these checks use. What is
held: which client each call was made with, and which workspace's turns a
command reads.
"""

from conformance_kit import ModelTurn
from slack_transport import SECOND_TEAM, TEAM, dm_of
from test_slack_connector_commands import ASKER, COMMAND, ROOM, ROOT, SECOND, _link
from test_slack_workspaces import TWO, _sent, build, slack, two  # noqa: F401

from belle_agent.platforms.slack import commands as slack_commands
from belle_agent.platforms.slack.scope_policy import DEFAULT_BOT_NAME

WELCOMING = TWO.replace("enforce_audience: true", "enforce_audience: true\n    home_tab_welcome: true")


def _message(text: str, ts: str, **fields) -> dict:
    return {"type": "message", "channel": ROOM, "channel_type": "channel", "user": ASKER, "text": text, "ts": ts, **fields}


async def _slash(socket, text: str):
    return await socket.app.slash(COMMAND, text, user=ASKER, channel=ROOM)


async def test_a_command_acts_in_the_workspace_it_was_typed_in_and_calls_no_other(two):  # noqa: F811
    world, first, second = await two(WELCOMING)
    world.kit.play(ModelTurn(final="the private answer"))
    quiet = list(first.web.calls)

    asked = await _slash(second, "ask what is the plan")
    await world.host.idle()
    opened = await second.app.deliver(
        {"type": "app_home_opened", "user": ASKER, "channel": dm_of(ASKER), "tab": "home", "event_ts": "1710000400.000500"}
    )
    chosen = (await _slash(second, "models use other-model")).texts[-1]

    # The question and its answer are in the second workspace's direct message, and its turn is that workspace's.
    assert asked.texts == [slack_commands.ask_answering_in_dm_notice(DEFAULT_BOT_NAME)]
    assert _sent(second) == [(ASKER, _sent(second)[0][1]), (dm_of(ASKER), "the private answer")]
    assert world.requests[0].conversation.startswith(f"slack_{SECOND_TEAM}_{dm_of(ASKER)}_")
    assert opened == "welcomed:empty-home-tab" and second.web.payload("views_publish")["user_id"] == ASKER
    assert chosen.startswith("Selected") and world.host.model_choices == {(SECOND_TEAM, ROOM): "other-model"}
    # Nothing of this was done with the first workspace's client, and nothing was answered through its app.
    assert first.web.calls == quiet and first.app.responses == []


async def test_a_command_reads_and_stops_only_the_turns_of_its_own_workspace(two):  # noqa: F811
    world, first, second = await two()
    world.kit.play(ModelTurn(final="first", hold=True))
    await first.app.deliver(_message("one", ROOT))
    await world.kit.holding()
    assert (await first.app.deliver(_message("two", SECOND, thread_ts=ROOT))).startswith("queued")
    # The second workspace's room has the same id, and its kind is learned from a message there.
    await second.app.deliver({**_message("elsewhere", ROOT), "bot_id": "B0ANOTHERAPP"})

    # From the other workspace the room is empty: nothing to list, to remove or to stop.
    assert "nothing running" in (await _slash(second, "status")).texts[-1]
    assert (await _slash(second, "queue list")).texts[-1] == "No messages are waiting in this channel or direct message."
    assert (await _slash(second, "stop")).texts[-1] == slack_commands.STOP_NONE_NOTICE
    assert (await _slash(second, f"stop {_link(ROOT)}")).texts[-1] == slack_commands.STOP_NONE_NOTICE
    assert (await _slash(second, f"queue drop {_link(SECOND)}")).texts[-1] == "That message is not waiting in this conversation."
    assert world.outcomes() == [] and len(world.host.waiting(TEAM, ROOM)) == 1

    # Control: the same commands in the workspace of the turn.
    assert "1 turn running" in (await _slash(first, "status")).texts[-1] and "1 message waiting" in (await _slash(first, "status")).texts[-1]
    assert (await _slash(first, f"queue drop {_link(SECOND)}")).texts[-1].startswith("Removed the waiting message")
    assert (await _slash(first, f"stop {_link(ROOT)}")).texts[-1] == slack_commands.STOP_ACCEPTED_NOTICE
    world.kit.release()
    await world.host.idle()
    assert world.outcomes() == ["interrupted"]
