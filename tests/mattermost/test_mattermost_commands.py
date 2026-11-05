"""The slash command: its token, and each subcommand's answer, given back in the request to its sender alone."""

import json
from types import SimpleNamespace

import httpx
import pytest
from transport import TOKEN, URL, EventFeeder, FakeRest, hello

from belle_agent.backend import BackendCapabilities, Catalog, TurnScope
from belle_agent.models import ConfiguredModels
from belle_agent.platforms.base import InboundDrop, InboundStop
from belle_agent.platforms.mattermost import commands
from belle_agent.platforms.mattermost.client import MattermostClient
from belle_agent.platforms.mattermost.connector import MattermostPlatform
from belle_agent.platforms.mattermost.settings import MattermostSettings

CHANNEL, TEAM, ALICE = "c" * 26, "t" * 26, "a" * 26
COMMAND_TOKEN = "synthetic-command-token-0123"
RUNNING, WAITING = "r" * 26, "w" * 26


def _turn(message_id: str, text: str = "a long question") -> SimpleNamespace:
    return SimpleNamespace(inbound=SimpleNamespace(message_id=message_id, sender=ALICE, text=text))


class Host:
    """The host's command operations, as a connector's command surface reaches them."""

    backend_capabilities = BackendCapabilities(
        backend="scripted", catalog=frozenset({"models", "skills", "subagents", "experts"}),
        scope_fields=frozenset({"skills_required", "subagent_required"}),
    )

    def __init__(self) -> None:
        self.model_choices: dict = {}
        self.running_turns = [_turn(RUNNING)]
        self.allowed = True
        self.scope = TurnScope(skills_available=frozenset({"notes"}), expert="planner")
        self.direct_scope: TurnScope | None = None

    def admits(self, user):
        return self.allowed

    def serves(self, channel):
        return True

    def command_unavailable(self, words):
        return ""

    def running(self, workspace, conversation):
        return self.running_turns

    def waiting(self, workspace, conversation):
        return [_turn(WAITING, "later please")]

    def held(self, workspace, conversation):
        return 1

    def settled(self, at):
        return self.direct_scope if at.chat_type == "D" and self.direct_scope else self.scope

    def scoped_model(self, at):
        return ""

    async def catalog(self):
        return Catalog(
            skills=({"name": "notes", "description": "Keep notes.", "installed": True}, {"name": "hidden", "installed": True}),
            experts=("planner",), subagents=("reviewer",), models=ConfiguredModels(names=("fast", "deep")),
        )


@pytest.fixture
async def connected():
    rest = FakeRest()
    rest.channels[CHANNEL] = {"id": CHANNEL, "type": "O", "team_id": TEAM, "name": "lounge"}
    client = MattermostClient(URL, TOKEN, transport=rest.transport(), open_events=EventFeeder(hello()).open)
    settings = MattermostSettings(URL, TOKEN, actions_url="http://belle.invalid:18400", command_token=COMMAND_TOKEN)
    connector = MattermostPlatform(settings, client=client)
    host, records = Host(), []

    async def receive(record):
        records.append(record)
        return {InboundStop: "stopped session=s1", InboundDrop: "removed:waiting-message"}.get(type(record), "dispatched:s2")

    connector.bind(host)
    await connector.start(receive)
    yield connector, host, records
    await connector.stop()


async def _say(connector, text: str, token: str = COMMAND_TOKEN) -> str:
    answer = await connector.commands.on_command(
        {"user_id": ALICE, "user_name": "alice", "channel_id": CHANNEL, "team_id": TEAM, "command": "/belle", "text": text},
        f"Token {token}",
    )
    assert answer["response_type"] == "ephemeral"
    return answer["text"]


async def test_a_command_without_the_token_is_refused_and_reaches_nothing(connected):
    connector, _, records = connected

    assert await _say(connector, "stop", token="guess") == commands.TOKEN_REFUSED
    assert records == []


async def test_help_lists_every_subcommand_with_the_command_s_name(connected):
    connector, _, _ = connected

    told = await _say(connector, "")

    assert all(f"`/belle {sub.name}" in told for sub in commands.SUBCOMMANDS)


async def test_schedule_command_uses_the_authenticated_mattermost_channel(connected):
    connector, host, _ = connected
    calls = []

    class Toolkit:
        async def invoke_at(self, name, arguments, *, at):
            calls.append((name, arguments, at))
            return json.dumps({"ok": True, "jobs": []})

    host.schedule_toolkit = Toolkit()
    assert "No scheduled jobs" in await _say(connector, "schedule list")
    name, arguments, at = calls[0]
    assert (name, arguments, at.platform, at.workspace, at.conversation, at.sender) == (
        "schedule_list", {}, "mattermost", TEAM, CHANNEL, ALICE
    )


async def test_stop_hands_the_host_a_stop_for_the_one_running_turn(connected):
    connector, host, records = connected

    assert await _say(connector, "stop") == "Stopping this turn."
    assert records == [InboundStop("mattermost", TEAM, CHANNEL, RUNNING, ALICE)]

    host.running_turns = []
    assert await _say(connector, "stop") == "No running turn was found in this channel."


async def test_the_queue_is_listed_and_a_message_of_it_removed_by_its_link(connected):
    connector, _, records = connected

    assert f"`{WAITING}` by {ALICE}: later please" in await _say(connector, "queue list")
    assert await _say(connector, f"queue drop http://chat.invalid/team/pl/{WAITING}") == "Removed that waiting message."
    assert records == [InboundDrop("mattermost", TEAM, CHANNEL, WAITING, ALICE)]


async def test_status_and_models_answer_from_the_host(connected):
    connector, host, _ = connected

    status = await _say(connector, "status")
    assert "**Agent**: running" in status and "1 turn running, 1 message waiting" in status and "1 tool call" in status
    assert "`fast`" in await _say(connector, "models list")
    assert await _say(connector, "models use") == "Usage: `/belle models use <name>`"
    assert (await _say(connector, "models use deep")).startswith("Selected `deep`")
    assert host.model_choices == {(TEAM, CHANNEL): "deep"}


async def test_a_person_the_deployment_does_not_admit_is_told_so(connected):
    connector, host, records = connected
    host.allowed = False

    assert await _say(connector, "stop") == commands.NOT_PERMITTED and records == []


async def test_the_server_posts_the_command_as_a_form_with_its_token_in_the_header(connected):
    connector, _, _ = connected
    form = {"token": COMMAND_TOKEN, "user_id": ALICE, "channel_id": CHANNEL, "command": "/belle", "text": "help"}

    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{connector.actions.port}") as server:
        answered = await server.post("/command", data=form, headers={"Authorization": f"Token {COMMAND_TOKEN}"})
        field_only = await server.post("/command", data=form)

    assert answered.status_code == 200 and answered.json()["text"].startswith("**Belle commands**")
    assert field_only.json()["text"] == commands.TOKEN_REFUSED


async def test_bg_uses_the_server_trigger_id_for_retry_identity(connected):
    connector, host, _ = connected

    class Sessions:
        async def invoke_at(self, name, arguments, *, at):
            assert name == "session_start" and arguments == {"prompt": "write a note"}
            assert at.event_id == "stable-trigger"
            return json.dumps({"ok": True, "session_id": "ses-0123456789", "state": "pending"})

    host.session_toolkit = Sessions()
    answer = await connector.commands.on_command(
        {"user_id": ALICE, "channel_id": CHANNEL, "command": "/belle", "text": "bg write a note",
         "trigger_id": "stable-trigger"},
        f"Token {COMMAND_TOKEN}",
    )
    assert "ses-0123456789" in answer["text"]


async def test_with_no_token_set_every_command_is_refused():
    rest = FakeRest()
    client = MattermostClient(URL, TOKEN, transport=rest.transport(), open_events=EventFeeder(hello()).open)
    connector = MattermostPlatform(MattermostSettings(URL, TOKEN, actions_url="http://belle.invalid:18400"), client=client)

    answer = await connector.commands.on_command({"user_id": ALICE, "channel_id": CHANNEL, "text": "help"}, "Token ")

    assert answer["text"] == commands.TOKEN_UNSET


async def test_the_lists_answer_from_the_backend_s_catalog_as_the_scope_narrows_it(connected):
    connector, _, _ = connected

    skills = await _say(connector, "skills list")
    assert "`notes`: Keep notes" in skills and "hidden" not in skills
    assert "which the scripted backend does not apply to a turn" in skills
    assert "`reviewer`" in await _say(connector, "subagents list")
    assert "**Selected by scope**: `planner`" in await _say(connector, "experts list")


async def test_a_skill_the_scope_makes_unavailable_is_neither_listed_nor_required(connected):
    connector, host, records = connected
    host.scope = TurnScope(skills_unavailable=frozenset({"hidden"}))

    skills = await _say(connector, "skills list")
    assert "`notes`: Keep notes" in skills and "hidden" not in skills
    assert "`agent.skills.unavailable`, which the scripted backend does not apply to a turn" in skills
    assert (await _say(connector, "skills use hidden x")).startswith("No skill called `hidden` can be used here.")

    # Usable where the command was typed, and unavailable in the direct channel, where the turn would run.
    host.direct_scope = TurnScope(skills_unavailable=frozenset({"notes"}))
    count = len(records)
    assert "`notes` is not allowed in your direct message with @belle" in await _say(connector, "skills use notes x")
    assert len(records) == count


async def test_a_question_asked_in_a_channel_opens_a_conversation_of_its_own_in_the_bot_s_direct_channel(connected):
    connector, _, records = connected

    told = await _say(connector, "ask what changed?")

    asked = records[-1]
    assert told == "Answering in your direct message with @belle. Nothing is posted here."
    assert asked.chat_type == "D" and asked.workspace == "direct" and asked.conversation != CHANNEL
    assert asked.side == asked.thread == asked.reply_thread == asked.message_id
    assert asked.text.startswith("[Asked privately from Mattermost channel ~lounge with /belle ask.")
    assert asked.text.endswith("what changed?")

    reply = {"id": "f" * 26, "channel_id": asked.conversation, "user_id": ALICE, "root_id": asked.message_id, "message": "and then?"}
    await connector.handle({"event": "posted", "data": {"post": json.dumps(reply), "channel_type": "D"}})
    assert records[-1].side == asked.message_id


async def test_a_skill_or_a_subagent_is_required_of_the_private_turn_only_where_the_scope_allows_it(connected):
    connector, host, records = connected

    assert (await _say(connector, "skills use notes sum up the week")).startswith("Answering in your direct message")
    assert records[-1].skills_required == ("notes",) and records[-1].text.endswith("sum up the week")
    assert (await _say(connector, "skills use hidden x")).startswith("No skill called `hidden` can be used here.")
    assert await _say(connector, "skills use notes") == "Usage: `/belle skills use <skill> <task>`"
    assert (await _say(connector, "subagents use reviewer read this")).startswith("Answering")
    assert records[-1].subagent_required == ("reviewer", "read this")

    host.direct_scope = TurnScope(subagents_available=frozenset())
    assert "is not allowed in your direct message with @belle" in await _say(connector, "subagents use reviewer x")
    assert await _say(connector, "ask") == "Usage: `/belle ask <question>`"
