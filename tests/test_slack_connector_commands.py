"""The slash command of the Slack connector, on a Slack that exists only in this process.

A command arrives as the payload Socket Mode delivers, through the listener
the connector registered. What a person was shown is read from the
acknowledgement and from the posts to the ``response_url``. What the host did
is read from the backend's requests and from the web API calls. No connection
is opened and the tokens are synthetic.
"""

import contextlib
import json
import logging

import pytest
import pytest_asyncio
from conformance_kit import ModelTurn, worlds
from scripted_backend import ScriptedBackend, ScriptedTurn
from slack_kit import SlackKit
from slack_transport import BOT_USER, TEAM, WORKSPACE_URL, button, click_body, dm_of, ephemeral_body
from test_slack_turn_surfaces import _cards, _stop_button, clock  # noqa: F401

from belle_agent.backend import SCOPE_FIELDS, BackendCapabilities
from belle_agent.click_notices import STOP_ACCEPTED_NOTICE
from belle_agent.config import BelleConfigError, load_config
from belle_agent.platforms.base import create_platform
from belle_agent.platforms.slack import commands as slack_commands
from belle_agent.platforms.slack import connector, task_forms
from belle_agent.platforms.slack.command_ui import (
    CANCELLED_NOTICE,
    MODEL_USE_SELECT_ACTION_ID,
    SLASH_QUEUE_DROP_SELECT_ID,
    SLASH_STOP_SELECT_ID,
)
from belle_agent.platforms.slack.connector_commands import other_command_notice
from belle_agent.platforms.slack.scope_policy import DEFAULT_BOT_NAME, DEFAULT_COMMAND_NAME, command_token
from belle_agent.platforms.slack.stream_delivery import STREAM_STOPPED_TAIL
from belle_agent.tools.host import APPROVE, AWAITING_CONFIRMATION

COMMAND = command_token(DEFAULT_COMMAND_NAME)
ROOM = "C0ROOM0001"
OTHER_ROOM = "C0ROOM0002"
ASKER = "U0ASKER001"
OTHER = "U0OTHER002"
LEAD = "U0LEAD0003"
DIRECT = dm_of(ASKER)
ROOT = "1710000001.000100"
SECOND = "1710000002.000100"
THIRD = "1710000003.000100"

ANSWER_EVERYTHING = """
scopes:
  - match: {channel: slack}
    delivery: {mode: [all]}
"""
A_LEAD_DECIDES = (
    ANSWER_EVERYTHING
    + f"""  - match: {{channel: slack, chat: {ROOM}}}
    clicks: {{stop: {{user: [{LEAD}]}}, queue_drop: {{user: [{LEAD}]}}}}
"""
)


def _link(ts: str, channel: str = ROOM) -> str:
    return f"{WORKSPACE_URL}archives/{channel}/p{ts.replace('.', '')}"


@pytest.fixture
def slack(monkeypatch) -> SlackKit:
    return SlackKit(monkeypatch)


@pytest_asyncio.fixture
async def build_world(slack, tmp_path):
    """A host on the connector with the scripted backend behind it, which declares what a check gives it."""
    async with contextlib.AsyncExitStack() as stack:

        async def build(settings: str = ANSWER_EVERYTHING, *, capabilities: BackendCapabilities | None = None):
            directory = tmp_path / f"world-{len(slack.sockets)}"
            directory.mkdir()
            make = await stack.enter_async_context(worlds(ScriptedBackend(capabilities), directory, slack))
            return await make(settings)

        yield build


async def _opencode_declares() -> BackendCapabilities:
    """What the OpenCode adapter declares, read from the adapter and from nowhere else."""
    from belle_agent.integrations.opencode.adapter import OpenCodeBackend

    adapter = OpenCodeBackend("http://opencode.invalid:4096", "synthetic")
    try:
        return adapter.capabilities()
    finally:
        await adapter.close()


async def _say(world, text: str, *, ts: str, user: str = ASKER, thread: str = "", channel: str = ROOM, kind: str = "channel") -> str:
    """One message of a person, as Slack delivers it."""
    event = {"type": "message", "channel": channel, "channel_type": kind, "user": user, "text": text, "ts": ts}
    if thread:
        event["thread_ts"] = thread
    return await world.platform.app.deliver(event, event_id=f"Ev{ts}")


async def _slash(world, text: str = "", *, user: str = ASKER, channel: str = ROOM, team: str = TEAM, command: str = COMMAND):
    return await world.platform.app.slash(command, text, user=user, channel=channel, team=team)


async def _one_runs_and_two_wait(world) -> None:
    """A held turn in one thread of the room, started by the asker, with a message of each person behind it."""
    world.kit.play(ModelTurn(final="first", hold=True))
    assert (await _say(world, "one", ts=ROOT)).startswith("dispatched")
    await world.kit.holding()
    assert (await _say(world, "two", ts=SECOND, user=OTHER, thread=ROOT)).startswith("queued")
    assert (await _say(world, "three", ts=THIRD, thread=ROOT)).startswith("queued")


def _select(exchange) -> dict:
    """The select menu of a picker a command answered with."""
    (payload,) = [said for said in exchange.said if said.get("blocks")]
    return payload["blocks"][1]["elements"][0]


def _button(blocks: list, action_id: str) -> dict:
    return next(
        element for block in blocks if block["type"] == "actions" for element in block["elements"] if element["action_id"] == action_id
    )


def _sent(world) -> list[tuple[str, str]]:
    """Every message the app posted, as ``(channel, text)``."""
    return [(str(payload.get("channel")), str(payload.get("text"))) for _, payload, _ in world.platform.web.sent]


# ── the command itself ───────────────────────────────────────────────────


async def test_help_names_every_subcommand_and_starts_nothing(build_world):
    world = await build_world()

    bare, asked = await _slash(world), await _slash(world, "help")

    assert bare.texts == asked.texts and len(bare.acks) == 1 and bare.responses == []
    for sub in slack_commands.SUBCOMMANDS:
        assert slack_commands.usage(sub, COMMAND) in bare.texts[0]
    assert "Not available here" not in bare.texts[0] and DEFAULT_BOT_NAME in bare.texts[0]
    # An answer to a command is the person's alone, and help asks nothing of Slack or of the backend.
    assert bare.acks[0]["response_type"] == "ephemeral"
    assert world.requests == [] and [name for name, _ in world.platform.web.calls] == ["auth_test"]


async def test_the_command_is_registered_and_quoted_under_the_configured_name(build_world):
    world = await build_world("platforms: {slack: {command_name: /Front-Desk, bot_name: Belle}}\n" + ANSWER_EVERYTHING)

    renamed = await _slash(world, "help", command="/front-desk")
    assert "`/front-desk status`" in renamed.texts[0] and "*Belle commands in Slack*" in renamed.texts[0]
    assert COMMAND not in renamed.texts[0]
    # Another command a manifest declares for the app is answered with the name that is served.
    assert (await _slash(world, "help", command="/another")).texts == [other_command_notice("/front-desk")]


def test_a_command_name_slack_would_refuse_stops_the_start(slack, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("platforms: {slack: {enforce_audience: true, command_name: 'two words'}}\n", encoding="utf-8")

    with pytest.raises(BelleConfigError, match="command_name is not a name Slack accepts"):
        create_platform(load_config(path), "slack")


def test_the_two_settings_are_acted_on_and_no_longer_named_as_doing_nothing(slack, tmp_path, caplog):
    path = tmp_path / "config.yaml"
    path.write_text("platforms: {slack: {enforce_audience: true, command_name: desk, home_tab_welcome: true}}\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="belle_agent.platforms.slack.connector"):
        platform = create_platform(load_config(path), "slack")

    assert caplog.records == []
    assert (platform.config.command_name, platform.config.home_tab_welcome) == ("desk", True)
    assert not {"command_name", "home_tab_welcome"} & connector.UNREAD_SETTINGS


async def test_what_is_not_a_subcommand_and_what_is_misused_are_answered_plainly(build_world):
    world = await build_world()

    unknown = await _slash(world, "dance now")
    misused = await _slash(world, "skills list everything")

    assert unknown.texts[0].startswith(f"That is not a `{COMMAND}` command.") and "dance" not in unknown.texts[0]
    assert misused.texts == [slack_commands.usage_text(slack_commands.SKILLS_LIST, COMMAND)]
    assert world.requests == []


async def test_who_may_not_use_the_command_is_told_so_and_nothing_else_happens(build_world):
    world = await build_world(f"platforms: {{slack: {{allow_from: [{ASKER}, {LEAD}], allowed_channel_ids: [{ROOM}]}}}}\n" + ANSWER_EVERYTHING)
    await _say(world, "hello", ts=ROOT)
    await world.host.idle()
    calls = len(world.platform.web.calls)

    stranger = await _slash(world, "ask what is planned", user=OTHER)
    elsewhere = await _slash(world, "ask what is planned", channel=OTHER_ROOM)
    foreign = await _slash(world, "ask what is planned", team="T0ANOTHER")

    assert stranger.texts == [slack_commands.not_permitted_notice(DEFAULT_BOT_NAME)]
    assert elsewhere.texts == [slack_commands.conversation_not_enabled_notice(DEFAULT_BOT_NAME)]
    assert foreign.texts == [slack_commands.foreign_workspace_notice(DEFAULT_BOT_NAME)]
    # A refusal is an absent side effect: no question was posted anywhere and no turn started.
    assert len(world.platform.web.calls) == calls and len(world.requests) == 1

    # Control: the same command from a person and a place that are served.
    assert (await _slash(world, "ask what is planned")).texts == [slack_commands.ask_answering_in_dm_notice(DEFAULT_BOT_NAME)]
    await world.host.idle()
    assert len(world.requests) == 2


# ── what a backend does not provide ──────────────────────────────────────

# What each subcommand answers on the OpenCode adapter's declaration. The README table is this list.
ON_OPENCODE_UNPROVIDED = {
    "subagents use": "the opencode backend cannot require a subagent of one turn",
    "experts list": "the opencode backend lists no experts",
}


@pytest.mark.opencode
async def test_a_subcommand_the_backend_cannot_serve_says_so_and_asks_it_nothing(build_world):
    world = await build_world(capabilities=await _opencode_declares())

    listed = (await _slash(world, "help")).texts[0]
    for sub in slack_commands.SUBCOMMANDS:
        reason = ON_OPENCODE_UNPROVIDED.get(sub.name)
        line = next(line for line in listed.splitlines() if f"`{slack_commands.usage(sub, COMMAND)}`" in line)
        assert ("Not available here" in line) == (reason is not None), line
        if reason:
            assert line.endswith(f"_Not available here: {reason}._")

    experts = await _slash(world, "experts list")
    delegated = await _slash(world, "subagents use researcher find the report")
    assert experts.texts == [f"`{COMMAND} experts list` is not available here: the opencode backend lists no experts."]
    assert delegated.texts == [
        f"`{COMMAND} subagents use <name> <task>` is not available here: the opencode backend cannot require a subagent of one turn."
    ]
    # Nothing was posted for either, and no turn started.
    assert world.requests == [] and world.platform.web.sent == []

    # Control: what the backend does provide is answered from its catalog.
    assert "`notes`" in (await _slash(world, "skills list")).texts[-1]
    assert "`researcher`" in (await _slash(world, "subagents list")).texts[-1]


async def test_a_backend_that_declares_nothing_leaves_the_host_commands_working(build_world):
    world = await build_world(capabilities=BackendCapabilities(backend="bare", scope_fields=SCOPE_FIELDS))

    assert (await _slash(world, "models list")).texts == [
        f"`{COMMAND} models list` is not available here: the bare backend lists no models."
    ]
    assert (await _slash(world, "skills use notes do it")).texts == [
        f"`{COMMAND} skills use <skill> <task>` is not available here: the bare backend lists no skills."
    ]
    # The queue, the stop and the private question are the host's own.
    assert (await _slash(world, "queue list")).texts == ["No messages are waiting in this channel or direct message."]
    assert (await _slash(world, "stop")).texts == [slack_commands.STOP_NONE_NOTICE]


@pytest.mark.opencode
async def test_a_list_says_when_the_backend_does_not_hold_a_turn_to_it(build_world):
    restricted = ANSWER_EVERYTHING + f"""  - match: {{channel: slack, chat: {ROOM}}}
    agent: {{skills: {{available: [notes]}}, subagents: [writer]}}
"""
    applied = await build_world(restricted)
    unapplied = await build_world(restricted, capabilities=await _opencode_declares())

    held = (await _slash(applied, "skills list")).texts[-1]
    told = (await _slash(unapplied, "skills list")).texts[-1]
    subagents = (await _slash(unapplied, "subagents list")).texts[-1]

    # The command follows the scope either way. Where the backend does not, the list says so.
    assert "`notes`" in held and "`digest`" not in held and "does not apply" not in held
    # OpenCode applies the skill list, so nothing is added to it.
    assert "`notes`" in told and "`digest`" not in told and "does not apply" not in told
    assert "`writer`" in subagents and "`researcher`" not in subagents
    assert "`agent.subagents`, which the opencode backend does not apply to a turn" in subagents
    # Where no scope restricts, the list is the whole catalog and there is nothing to add.
    assert "does not apply" not in (await _slash(unapplied, "subagents list", channel=DIRECT)).texts[-1]


# ── status ───────────────────────────────────────────────────────────────


async def test_status_shows_the_backend_the_model_and_what_runs_and_waits(build_world):
    world = await build_world()
    await _one_runs_and_two_wait(world)

    in_room = (await _slash(world, "status")).texts[-1]
    in_dm = (await _slash(world, "status", channel=DIRECT)).texts[-1]

    assert "*Agent*: running, version scripted 1 (status check took" in in_room
    assert "*Model*: `default-model`" in in_room
    assert f"1 turn running (<{_link(ROOT)}|source message>), 2 messages waiting" in in_room
    assert "A message sent during a turn waits until the current turn ends" in in_room
    assert f"*`{COMMAND} ask` here would use*: a new conversation, without this channel's history" in in_room
    # A direct message has no private question to send elsewhere, and none of the room's turns.
    assert "would use" not in in_dm and "nothing running" in in_dm

    world.kit.release()
    await world.host.idle()


async def test_status_says_when_the_backend_does_not_answer(build_world):
    world = await build_world()
    world.kit.listed = RuntimeError("down")

    status = (await _slash(world, "status")).texts[-1]

    assert "*Agent*: not answering" in status and "*Model*: unknown" in status


# ── catalogs ─────────────────────────────────────────────────────────────


async def test_the_lists_follow_the_catalog_and_the_scope_of_the_conversation(build_world):
    world = await build_world(
        ANSWER_EVERYTHING
        + f"""  - match: {{channel: slack, chat: {ROOM}}}
    agent: {{expert: reviewer, subagents: [writer], skills: {{available: [notes], required: [notes]}}}}
"""
    )

    skills = (await _slash(world, "skills list")).texts[-1]
    subagents = (await _slash(world, "subagents list")).texts[-1]
    experts = (await _slash(world, "experts list")).texts[-1]
    everywhere = (await _slash(world, "skills list", channel=DIRECT)).texts[-1]

    assert "(1)" in skills and "`notes` (always loaded here): Keep notes of a conversation" in skills and "digest" not in skills
    assert subagents == "*Subagents available in this conversation* (1)\n• `writer`"
    assert "*Selected by scope*: `reviewer`" in experts and "• `reviewer`" in experts
    # Control: the same list where no scope restricts.
    assert "(2)" in everywhere and "`digest`" in everywhere and "always loaded" not in everywhere


async def test_a_catalog_that_cannot_be_read_is_said_to_be_unreadable(build_world):
    world = await build_world()
    world.kit.listed = RuntimeError("down")

    assert (await _slash(world, "skills list")).texts == [slack_commands.SKILLS_UNAVAILABLE_NOTICE]
    assert (await _slash(world, "models list")).texts == ["The configured model list is unavailable. Try again later."]


# ── models ───────────────────────────────────────────────────────────────


async def test_a_model_chosen_by_command_serves_the_conversation_until_it_is_reset(build_world):
    world = await build_world()

    listed = (await _slash(world, "models list")).texts[-1]
    before = (await _slash(world, "models current")).texts[-1]
    unknown = (await _slash(world, "models use no-such-model")).texts[-1]
    chosen = (await _slash(world, "models use other-model")).texts[-1]
    during = (await _slash(world, "models current")).texts[-1]
    await _say(world, "with the chosen model", ts=ROOT)
    await _say(world, "in another conversation", ts=SECOND, channel=OTHER_ROOM)
    await world.host.idle()
    reset = (await _slash(world, "models reset")).texts[-1]
    await _say(world, "with the default again", ts=THIRD)
    await world.host.idle()

    assert listed == "Configured models:\n• `default-model`\n• `other-model`"
    assert before == "Model for your new requests here: `default-model` (configured default)."
    assert unknown.startswith("Unknown model `no-such-model`.")
    assert chosen.startswith("Selected `other-model` for new requests.")
    assert during == "Model for your new requests here: `other-model` (temporary selection)."
    assert reset.startswith("Temporary selection removed.")
    # The room's turn ran on the choice. The other room's did not, and neither did the room's next one.
    assert [request.scope.model for request in world.requests] == ["other-model", "", ""]


async def test_the_model_picker_offers_the_catalog_and_its_choice_is_checked_again(build_world):
    world = await build_world()
    body = ephemeral_body(user=ASKER, channel=ROOM)
    await _say(world, "so the room's kind is known", ts=ROOT)
    await world.host.idle()

    picker = _select(await _slash(world, "models use"))
    found = await world.platform.app.suggest({**body, "action_id": MODEL_USE_SELECT_ACTION_ID, "value": "OTHER"})
    picked = await world.platform.app.interact(body, {"action_id": MODEL_USE_SELECT_ACTION_ID, "selected_option": {"value": "other-model"}})
    forged = await world.platform.app.interact(body, {"action_id": MODEL_USE_SELECT_ACTION_ID, "selected_option": {"value": "smuggled"}})

    assert (picker["type"], picker["action_id"]) == ("external_select", MODEL_USE_SELECT_ACTION_ID)
    assert [option["value"] for option in found] == ["other-model"]
    assert picked.texts[-1].startswith("Selected `other-model`")
    assert forged.texts[-1].startswith("Unknown model `smuggled`.")
    assert world.host.model_choices == {(TEAM, ROOM): "other-model"}


async def test_a_scope_that_selects_the_model_is_not_overridden_by_the_command(build_world):
    world = await build_world(
        ANSWER_EVERYTHING + f"  - match: {{channel: slack, chat: {ROOM}}}\n    agent: {{model_name: other-model}}\n"
    )
    await _say(world, "so the room's kind is known", ts=ROOT)
    await world.host.idle()

    refused = (await _slash(world, "models use default-model")).texts[-1]
    current = (await _slash(world, "models current")).texts[-1]
    options = await world.platform.app.suggest(
        {**ephemeral_body(user=ASKER, channel=ROOM), "action_id": MODEL_USE_SELECT_ACTION_ID, "value": ""}
    )

    assert refused == "A scope selects your model here. Change the scope to use another model."
    assert current == "Model for your new requests here: `other-model` (scope)."
    assert options == [] and world.host.model_choices == {}
    # Control: the same command where no scope selects a model.
    assert (await _slash(world, "models use other-model", channel=DIRECT)).texts[-1].startswith("Selected")


# ── the queue ────────────────────────────────────────────────────────────


async def test_the_queue_is_listed_with_a_link_and_a_name_for_each_waiting_message(build_world):
    world = await build_world()
    assert (await _slash(world, "queue list")).texts == ["No messages are waiting in this channel or direct message."]
    await _one_runs_and_two_wait(world)

    listed = (await _slash(world, "queue list")).texts[-1]

    assert listed == (
        "Messages waiting in this channel or direct message (2):\n"
        f"• <{_link(SECOND)}|message> from A Person\n"
        f"• <{_link(THIRD)}|message> from A Person"
    )
    world.kit.release()
    await world.host.idle()


async def test_a_waiting_message_is_removed_by_its_link_by_its_sender_only(build_world):
    world = await build_world("platforms: {slack: {acknowledge_mode: reaction}}\n" + ANSWER_EVERYTHING)
    await _one_runs_and_two_wait(world)
    web = world.platform.web

    foreign = (await _slash(world, f"queue drop <{_link(SECOND)}>")).texts[-1]
    elsewhere = (await _slash(world, f"queue drop {_link(SECOND, OTHER_ROOM)}")).texts[-1]
    assert foreign == "Only the sender can remove that waiting message here."
    assert elsewhere == "Use a message link from this conversation."
    # Neither removed anything.
    assert len(world.host.waiting(TEAM, ROOM)) == 2

    removed = (await _slash(world, f"queue drop {_link(THIRD)}")).texts[-1]
    again = (await _slash(world, f"queue drop {_link(THIRD)}")).texts[-1]
    assert removed == "Removed the waiting message. Its turn will not start."
    assert again == "That message is not waiting in this conversation."
    # Its waiting mark and its acknowledgement came off, and the thread keeps the status of what still waits.
    taken_off = [(payload["timestamp"], payload["name"]) for payload in web.payloads("reactions_remove")]
    assert taken_off == [(THIRD, "hourglass_flowing_sand"), (THIRD, "eyes")]

    world.kit.release()
    await world.host.idle()
    # Control: the message that was left waiting ran. The removed one did not.
    assert [world.platform_kit.said(request.text) for request in world.requests] == ["one", "two"]


async def test_the_drop_picker_offers_only_what_the_person_may_remove(build_world):
    world = await build_world(A_LEAD_DECIDES)
    await _one_runs_and_two_wait(world)
    app = world.platform.app

    own = _select(await _slash(world, "queue drop", user=OTHER))
    all_of_them = _select(await _slash(world, "queue drop", user=LEAD))
    assert [option["text"]["text"] for option in own["options"]] == ["two"]
    assert [option["text"]["text"] for option in all_of_them["options"]] == ["two", "three"]
    assert own["action_id"] == SLASH_QUEUE_DROP_SELECT_ID and "A Person · 2024-03-09" in own["options"][0]["description"]["text"]

    # A choice made by somebody the rule does not name is checked again when it is made.
    theirs = {"action_id": SLASH_QUEUE_DROP_SELECT_ID, "selected_option": all_of_them["options"][1]}
    refused = await app.interact(ephemeral_body(user=OTHER, channel=ROOM), theirs)
    assert refused.texts == ["Only the sender can remove that waiting message here."]
    assert len(world.host.waiting(TEAM, ROOM)) == 2
    picked = await app.interact(ephemeral_body(user=LEAD, channel=ROOM), theirs)
    assert picked.texts == [f"Removed <{_link(THIRD)}|the waiting message>. Its turn will not start."]
    assert picked.responses[-1]["replace_original"] is True

    world.kit.release()
    await world.host.idle()
    assert [world.platform_kit.said(request.text) for request in world.requests] == ["one", "two"]


# ── stop ─────────────────────────────────────────────────────────────────


async def test_the_typed_stop_the_picker_and_slacks_button_are_held_to_one_rule(build_world):
    world = await build_world(A_LEAD_DECIDES)
    world.kit.play(ModelTurn(final="never said", hold=True))
    await _say(world, "a long task", ts=ROOT)
    await world.kit.holding()
    app = world.platform.app

    # Somebody the rule does not name, by each of the three ways.
    typed = (await _slash(world, f"stop {_link(ROOT)}", user=OTHER)).texts[-1]
    picker = (await _slash(world, "stop", user=OTHER)).texts[-1]
    button = await app.deliver({"type": "agent_session_stopped", "channel": ROOM, "thread_ts": ROOT, "user": OTHER})
    assert typed == picker == slack_commands.STOP_REFUSED_NOTICE
    assert button.startswith("refused:clicks.stop")
    # A refusal is an absent side effect: the turn still runs.
    assert world.outcomes() == [] and len(world.host.running(TEAM, ROOM)) == 1

    # Whoever started the turn may stop it, whatever the rule says. Here through the picker.
    select = _select(await _slash(world, "stop"))
    assert select["action_id"] == SLASH_STOP_SELECT_ID and [option["text"]["text"] for option in select["options"]] == ["a long task"]
    chosen = {"action_id": SLASH_STOP_SELECT_ID, "selected_option": select["options"][0]}
    # A choice replayed by somebody else is checked for them.
    assert (await app.interact(ephemeral_body(user=OTHER, channel=ROOM), chosen)).texts == [slack_commands.STOP_REFUSED_NOTICE]
    stopped = await app.interact(ephemeral_body(user=ASKER, channel=ROOM), chosen)
    world.kit.release()
    await world.host.idle()

    assert stopped.texts == [f"Stopping <{_link(ROOT)}|that turn>."]
    assert world.outcomes() == ["interrupted"] and world.platform.web.payloads("chat_postMessage") == []
    assert (await _slash(world, "stop")).texts == [slack_commands.STOP_NONE_NOTICE]


async def test_the_person_a_rule_names_stops_another_persons_turn_by_its_link(build_world):
    world = await build_world(A_LEAD_DECIDES)
    world.kit.play(ModelTurn(final="never said", hold=True))
    await _say(world, "a long task", ts=ROOT)
    await world.kit.holding()

    wrong = (await _slash(world, f"stop {_link(SECOND)}", user=LEAD)).texts[-1]
    malformed = (await _slash(world, "stop that one", user=LEAD)).texts[-1]
    assert (wrong, malformed) == (slack_commands.STOP_NONE_NOTICE, slack_commands.STOP_LINK_INVALID_NOTICE)
    assert world.outcomes() == []

    stopped = (await _slash(world, f"stop {_link(ROOT)}", user=LEAD)).texts[-1]
    world.kit.release()
    await world.host.idle()
    assert stopped == slack_commands.STOP_ACCEPTED_NOTICE and world.outcomes() == ["interrupted"]


SHOWING = "platforms: {slack: {enable_streaming: true, activity_card: true}}\n"


@pytest.mark.parametrize("way", ["the card's button", "the command with a link", "the command's picker"])
async def test_a_turn_ends_what_it_showed_the_same_way_however_it_was_stopped(build_world, clock, way):  # noqa: F811
    world = await build_world(SHOWING + ANSWER_EVERYTHING)
    web, app = world.platform.web, world.platform.app
    world.kit.play(
        ScriptedTurn(narration="Looking it up.\n", final="never said", calls=(("react_to_message", {"emoji": "eyes"}),), hold_before_final=True)
    )
    await _say(world, "a long task", ts=ROOT)
    await world.kit.holding()
    await clock.advance(5)
    (card,) = _cards(web)
    before = len(web.calls)

    if way == "the card's button":
        body = click_body(user=ASKER, channel=ROOM, message_ts=card["ts"], payload=card)
        assert (await app.click(body, _stop_button(card))).startswith("stopped")
        assert web.payloads("chat_postEphemeral")[-1]["text"] == STOP_ACCEPTED_NOTICE
    elif way == "the command with a link":
        assert (await _slash(world, f"stop {_link(ROOT)}")).texts[-1] == slack_commands.STOP_ACCEPTED_NOTICE
    else:
        chosen = {"action_id": SLASH_STOP_SELECT_ID, "selected_option": _select(await _slash(world, "stop"))["options"][0]}
        assert (await app.interact(ephemeral_body(user=ASKER, channel=ROOM), chosen)).texts == [f"Stopping <{_link(ROOT)}|that turn>."]
    world.kit.release()
    await world.host.idle()

    # What the turn showed ends one way: the reply keeps what it wrote and says it was stopped, and the card is gone.
    assert world.outcomes() == ["interrupted"]
    assert web.shown(ROOM) == ["Looking it up.\n" + STREAM_STOPPED_TAIL] and web.streaming == set() and _cards(web) == []
    # The same calls end it, whichever way the stop was asked for. Only how the person is answered differs.
    after = [method for method, _ in web.calls[before:] if method not in ("chat_postEphemeral", "users_info")]
    assert after == ["chat_stopStream", "chat_delete", "agents.sessions.setStatus"]


# ── a private question ───────────────────────────────────────────────────


async def test_a_question_asked_privately_from_a_room_is_answered_only_to_the_person_who_asked(build_world):
    world = await build_world()
    web = world.platform.web
    world.kit.play(ModelTurn(final="the private answer"), ModelTurn(final="the follow-up answer"), ModelTurn(final="said in the room"))

    asked = await _slash(world, "ask what is the plan")
    await world.host.idle()

    # In the room, the person alone is told where the answer goes: an ephemeral answer to their command.
    assert asked.texts == [slack_commands.ask_answering_in_dm_notice(DEFAULT_BOT_NAME)]
    assert all(payload["response_type"] == "ephemeral" for payload in asked.said)
    (anchor, answer) = web.sent
    assert anchor[1]["channel"] == ASKER and anchor[1]["text"].startswith(f"You asked in <#{ROOM}> with `{COMMAND} ask`:\n>what is the plan")
    assert "This deployment does not copy a channel's conversation" in anchor[1]["text"]
    # The answer is threaded under the question, in the person's direct message.
    assert (answer[1]["channel"], answer[1]["thread_ts"], answer[1]["text"]) == (DIRECT, anchor[2], "the private answer")
    # The turn ran as a direct message of the person who asked, in a conversation of its own.
    (request,) = world.requests
    assert request.conversation == f"slack_{TEAM}_{DIRECT}_{ASKER}_{anchor[2]}"
    assert request.text.startswith(f"[Asked privately from Slack channel <#{ROOM}> with {COMMAND} ask.")
    assert request.metadata["slack_channel_id"] == DIRECT and request.metadata["slack_user_id"] == ASKER
    # A refusal is an absent call: nothing of this exchange names the room.
    assert ROOM not in {channel for channel, _ in _sent(world)}
    assert not [payload for payload in web.payloads("chat_postEphemeral") if payload["channel"] == ROOM]

    # A reply in the question's thread continues that conversation. The room hears nothing of it either.
    await _say(world, "and after that?", ts=SECOND, thread=anchor[2], channel=DIRECT, kind="im")
    await world.host.idle()
    assert world.requests[1].conversation == request.conversation
    assert _sent(world)[-1] == (DIRECT, "the follow-up answer") and web.sent[-1][1]["thread_ts"] == anchor[2]

    # Control: a message in the room is answered in the room.
    await _say(world, "and for everybody?", ts=THIRD)
    await world.host.idle()
    assert _sent(world)[-1] == (ROOM, "said in the room")
    assert world.requests[2].conversation == f"slack_{TEAM}_{ROOM}_{THIRD}"


async def test_what_a_private_turn_shows_while_it_runs_appears_only_in_the_persons_direct_message(build_world, clock):  # noqa: F811
    world = await build_world(SHOWING + ANSWER_EVERYTHING)
    web = world.platform.web
    working = ScriptedTurn(narration="Looking it up.\n", final="the private answer", calls=(("react_to_message", {"emoji": "eyes"}),), hold_before_final=True)
    world.kit.play(working, working)

    await _slash(world, "ask what is the plan")
    await world.kit.holding()
    await clock.advance(5)
    # The card and the reply as it is written are on screen now, both under the question in the direct message.
    (anchor, reply, card) = web.screen.values()
    assert anchor["channel"] == ASKER and _cards(web)[0]["channel"] == card["channel"] == reply["channel"] == DIRECT
    assert reply["text"] == "Looking it up.\n" and card["thread_ts"] == reply["thread_ts"] == web.sent[0][2]
    world.kit.release()
    await world.host.idle()

    # Nothing the turn showed, and no call that shows or marks anything, names the room.
    writes = [payload for method, payload in web.calls if method != "conversations_info"]
    assert not [payload for payload in writes if ROOM in (payload.get("channel"), payload.get("channel_id"))]
    assert {message["channel"] for message in web.screen.values()} == {ASKER, DIRECT}

    # Control: the same turn asked for in the room shows its card there.
    await _say(world, "and for everybody?", ts=THIRD)
    await world.kit.holding()
    await clock.advance(5)
    assert _cards(web)[-1]["channel"] == ROOM
    world.kit.release()
    await world.host.idle()


async def test_a_private_turn_that_would_post_into_the_room_asks_in_the_direct_message_first(build_world):
    world = await build_world()
    web = world.platform.web
    web.members.update({ROOM: [ASKER, OTHER, BOT_USER], DIRECT: [ASKER, BOT_USER]})
    world.kit.play(ModelTurn(final="I asked before posting there.", calls=(("post_message", {"text": "for the room", "chat_id": ROOM}),)))

    await _slash(world, "ask tell the room")
    await world.host.idle()

    # The post waits. The room has a reader the direct message has not, and the question says so.
    waiting = json.loads(world.kit.results[0])
    assert waiting["error"] == AWAITING_CONFIRMATION and OTHER in waiting["detail"]
    question = next(payload for payload in web.payloads("chat_postMessage") if payload.get("blocks"))
    asked_ts = next(ts for _, payload, ts in web.sent if payload is question)
    # It is asked under the private question, where only the person who asked reads it. The room sees nothing.
    assert (question["channel"], question["thread_ts"]) == (DIRECT, web.sent[0][2])
    assert ROOM not in {channel for channel, _ in _sent(world)}
    assert "*Waiting for an answer*: 1 tool call held until a person agrees." in (await _slash(world, "status", channel=DIRECT)).texts[-1]
    assert "Waiting for an answer" not in (await _slash(world, "status")).texts[-1]

    # The person agrees, and only then does the room get the post.
    body = click_body(user=ASKER, channel=DIRECT, message_ts=asked_ts, payload=question)
    assert (await world.platform.app.click(body, button(question["blocks"], value=APPROVE))).startswith("answered")
    assert _sent(world)[-1] == (ROOM, "for the room")
    assert "Waiting for an answer" not in (await _slash(world, "status", channel=DIRECT)).texts[-1]


async def test_a_question_asked_in_the_direct_message_uses_its_conversation(build_world):
    world = await build_world()
    world.kit.play(ModelTurn(final="the answer"))

    asked = await _slash(world, "ask what did I say before", channel=DIRECT)
    await world.host.idle()

    (anchor, answer) = world.platform.web.sent
    assert asked.said == []
    assert (anchor[1]["channel"], anchor[1]["text"]) == (DIRECT, f"You asked with `{COMMAND} ask`:\n>what did I say before")
    assert (answer[1]["channel"], answer[1].get("thread_ts"), answer[1]["text"]) == (DIRECT, None, "the answer")
    # The person's own direct-message conversation, and the question as they wrote it.
    assert world.requests[0].conversation == f"slack_{TEAM}_{DIRECT}_{ASKER}"
    assert world.platform_kit.said(world.requests[0].text) == "what did I say before"


async def test_a_question_that_cannot_be_posted_asks_nothing(build_world):
    world = await build_world()
    world.platform.web.fail = {"chat_postMessage": "channel_not_found"}

    asked = await _slash(world, "ask anything")
    await world.host.idle()

    assert asked.texts == [slack_commands.ask_answering_in_dm_notice(DEFAULT_BOT_NAME), slack_commands.ASK_NOT_POSTED_NOTICE]
    assert world.requests == []


async def test_the_question_form_is_submitted_once_and_only_by_whoever_opened_it(build_world):
    world = await build_world()
    app = world.platform.app
    await _say(world, "so the room's kind is known", ts=ROOT)
    await world.host.idle()
    world.kit.play(ModelTurn(final="the private answer"))

    form = (await _slash(world, "ask")).said[-1]
    nonce = _button(form["blocks"], task_forms.SUBMIT_ACTION)["value"]
    state = {task_forms.TASK_BLOCK: {task_forms.TASK_ACTION: {"value": "what is planned"}}}
    submit = {"action_id": task_forms.SUBMIT_ACTION, "value": nonce}

    empty = await app.interact(ephemeral_body(user=ASKER, channel=ROOM), submit)
    stranger = await app.interact(ephemeral_body(user=OTHER, channel=ROOM, state=state), submit)
    assert empty.texts == ["Enter a question before submitting."]
    assert stranger.texts == [slack_commands.not_permitted_notice(DEFAULT_BOT_NAME)]
    assert len(world.requests) == 1

    sent = await app.interact(ephemeral_body(user=ASKER, channel=ROOM, state=state), submit)
    again = await app.interact(ephemeral_body(user=ASKER, channel=ROOM, state=state), submit)
    await world.host.idle()

    assert form["text"] == "Enter a question for a private answer." and form["response_type"] == "ephemeral"
    assert sent.texts == ["Submitting your request.", slack_commands.ask_answering_in_dm_notice(DEFAULT_BOT_NAME)]
    assert again.texts == ["This form has expired. Run the command again."]
    assert len(world.requests) == 2 and "what is planned" in world.requests[1].text
    assert _sent(world)[-1] == (DIRECT, "the private answer")

    # A form that is cancelled submits nothing.
    other = (await _slash(world, "ask")).said[-1]
    cancel = {"action_id": task_forms.CANCEL_ACTION, "value": _button(other["blocks"], task_forms.CANCEL_ACTION)["value"]}
    assert (await app.interact(ephemeral_body(user=ASKER, channel=ROOM, state=state), cancel)).texts == [CANCELLED_NOTICE]
    assert len(world.requests) == 2


# ── one private task with a skill or a subagent ──────────────────────────


async def test_a_skill_required_by_command_is_required_of_one_private_turn(build_world):
    world = await build_world()
    world.kit.play(ModelTurn(final="the notes"), ModelTurn(final="later"))

    started = await _slash(world, "skills use NOTES summarize the meeting")
    await world.host.idle()
    await _say(world, "and later, plainly", ts=SECOND, channel=DIRECT, kind="im")
    await world.host.idle()

    anchor = world.platform.web.sent[0]
    assert started.texts == [slack_commands.ask_answering_in_dm_notice(DEFAULT_BOT_NAME)]
    assert anchor[1]["channel"] == ASKER and f"`{COMMAND} skills use notes`" in anchor[1]["text"]
    assert [request.scope.skills_required for request in world.requests] == [("notes",), ()]
    assert world.requests[0].conversation.endswith(f"_{anchor[2]}")
    assert ROOM not in {channel for channel, _ in _sent(world)}


async def test_a_skill_the_scope_does_not_allow_is_not_started(build_world):
    world = await build_world(
        ANSWER_EVERYTHING
        + f"""  - match: {{channel: slack, chat: {ROOM}}}
    agent: {{skills: {{available: [notes]}}}}
  - match: {{channel: slack, chat_type: im}}
    agent: {{skills: {{available: [digest]}}}}
"""
    )
    web = world.platform.web

    here = await _slash(world, "skills use digest summarize")
    assert here.texts == [slack_commands.skill_unknown_text("digest", COMMAND)]
    assert web.sent == [] and world.requests == []

    # Allowed where it was typed, and not where the turn would run: the question is withdrawn there.
    there = await _slash(world, "skills use notes summarize")
    await world.host.idle()
    refusal = slack_commands.skill_not_in_dm_text("notes", bot_name=DEFAULT_BOT_NAME)
    assert there.texts == [slack_commands.ask_answering_in_dm_notice(DEFAULT_BOT_NAME), refusal]
    assert web.payload("chat_update")["text"] == refusal and world.requests == []


async def test_a_skill_the_scope_makes_unavailable_is_neither_listed_nor_started(build_world):
    world = await build_world(
        ANSWER_EVERYTHING
        + f"""  - match: {{channel: slack, chat: {ROOM}}}
    agent: {{skills: {{unavailable: [digest]}}}}
  - match: {{channel: slack, chat_type: im}}
    agent: {{skills: {{unavailable: [notes]}}}}
"""
    )
    web = world.platform.web

    listed = (await _slash(world, "skills list")).texts[-1]
    in_direct = (await _slash(world, "skills list", channel=DIRECT)).texts[-1]
    assert "(1)" in listed and "`notes`" in listed and "digest" not in listed
    assert "(1)" in in_direct and "`digest`" in in_direct and "notes" not in in_direct

    here = await _slash(world, "skills use digest summarize")
    assert here.texts == [slack_commands.skill_unknown_text("digest", COMMAND)]
    assert web.sent == [] and world.requests == []

    # Usable where it was typed, and unavailable where the turn would run: the question is withdrawn there.
    there = await _slash(world, "skills use notes summarize")
    await world.host.idle()
    refusal = slack_commands.skill_not_in_dm_text("notes", bot_name=DEFAULT_BOT_NAME)
    assert there.texts == [slack_commands.ask_answering_in_dm_notice(DEFAULT_BOT_NAME), refusal]
    assert world.requests == []


async def test_the_skill_form_offers_the_names_the_scope_allows(build_world):
    world = await build_world()
    world.kit.play(ModelTurn(final="done"))
    await _say(world, "so the room's kind is known", ts=ROOT)
    await world.host.idle()

    form = (await _slash(world, "skills use digest")).said[-1]
    name = next(block for block in form["blocks"] if block.get("block_id") == task_forms.NAME_BLOCK)["element"]
    state = {
        task_forms.NAME_BLOCK: {task_forms.NAME_ACTION: {"selected_option": {"value": "notes"}}},
        task_forms.TASK_BLOCK: {task_forms.TASK_ACTION: {"value": "write them up"}},
    }
    submit = {"action_id": task_forms.SUBMIT_ACTION, "value": _button(form["blocks"], task_forms.SUBMIT_ACTION)["value"]}
    await world.platform.app.interact(ephemeral_body(user=ASKER, channel=ROOM, state=state), submit)
    await world.host.idle()

    assert [option["value"] for option in name["options"]] == ["notes", "digest"] and name["initial_option"]["value"] == "digest"
    assert world.requests[-1].scope.skills_required == ("notes",)


async def test_a_task_given_to_a_subagent_by_command_reaches_the_turn_as_its_requirement(build_world):
    world = await build_world()
    world.kit.play(ModelTurn(final="the report"))

    started = await _slash(world, "subagents use researcher find the report")
    unknown = await _slash(world, "subagents use nobody find the report")
    await world.host.idle()

    anchor = world.platform.web.sent[0]
    assert started.texts == [slack_commands.subagent_starting_in_dm_notice(DEFAULT_BOT_NAME)]
    assert unknown.texts == [slack_commands.subagent_unknown_text("nobody", COMMAND)]
    assert "The subagent receives only the task above." in anchor[1]["text"]
    (request,) = world.requests
    assert request.scope.subagent_required == ("researcher", "find the report")
    assert _sent(world)[-1] == (DIRECT, "the report")


# ── the log ──────────────────────────────────────────────────────────────


async def test_a_command_is_logged_by_its_outcome_and_not_by_its_text(build_world, caplog):
    world = await build_world()
    with caplog.at_level(logging.INFO):
        await _slash(world, "ask my secret plan")
        await world.host.idle()

    lines = [record.getMessage() for record in caplog.records if COMMAND in record.getMessage()]
    assert any("asked:fresh dispatched:dm" in line and ASKER in line for line in lines), lines
    assert "secret plan" not in caplog.text
    # Nothing a command logs is under an integration's name, or carries the JiuwenSwarm channel's tag.
    assert not [record for record in caplog.records if "integrations" in record.name or "[SlackChannel]" in record.getMessage()]

async def test_an_answer_slack_does_not_accept_is_logged_without_what_was_sent(build_world, caplog):
    from slack_sdk.webhook import WebhookResponse
    from test_slack_log_lines import _lines

    world = await build_world()
    app = world.platform.app
    with _lines(caplog, "belle_agent.platforms.slack.command_ui") as lines:
        # Slack answers a response URL it no longer takes with a status and a word, and Bolt raises nothing.
        app.respond_answer = WebhookResponse(url="https://hooks.slack.test/", status_code=404, body="expired_url", headers={})
        sent = (await _slash(world, "status")).texts[-1]
        # Control: an answer Slack takes writes no such line.
        app.respond_answer = WebhookResponse(url="https://hooks.slack.test/", status_code=200, body="ok", headers={})
        await _slash(world, "status")
        warnings = [line for level, line in lines() if level == "WARNING"]

    line = (
        f"Slack {COMMAND} answer was not accepted by Slack: team={TEAM} channel={ROOM} user={ASKER}"
        f" status=404 slack_said=expired_url sent_chars={len(sent)}"
    )
    assert warnings == [line]
    assert sent not in caplog.text
