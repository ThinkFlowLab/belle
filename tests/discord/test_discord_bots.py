"""Two Discord applications never borrow another installation's transport or session."""

import asyncio
import json

import pytest
from discord_transport import FakeGateway, FakeRest, hello, ready

from belle_agent.backend import Question, ToolCall
from belle_agent.platforms import installations
from belle_agent.platforms.discord.bots import DiscordBots
from belle_agent.platforms.discord.client import DiscordClient
from belle_agent.platforms.discord.connector import DiscordPlatform
from belle_agent.platforms.discord.settings import DiscordSettings
from belle_agent.platforms.installations import InstallationConnecting, InstallationUnknown
from belle_agent.tools.host import BoundTurn, ToolHost, request_metadata

GUILD = "1000000000000000200"
CHANNEL = "1000000000000000300"
DM = "1000000000000000500"
ALICE = "1000000000000000700"
MESSAGE = "1000000000000000800"


def _two_bots():
    """The connectors of two bots, east and west, each on a fake Discord of its own. None is started."""
    members, transports, gateways = [], [], []
    for index, name in enumerate(("east", "west")):
        rest = FakeRest(serve=True)
        rest.gateway()
        rest.channels[CHANNEL] = {"id": CHANNEL, "guild_id": GUILD, "type": 0}
        rest.channels[DM] = {"id": DM, "type": 1}
        payload = ready()
        payload["d"]["user"]["id"] = str(1000000000000000116 + index)
        payload["d"]["application"]["id"] = payload["d"]["user"]["id"]

        def opens(socket, payload=payload):
            socket.feed(hello())
            socket.feed(payload)

        gateway = FakeGateway(opens)
        token = f"synthetic-token-{name}"
        client = DiscordClient(token, transport=rest.transport(), open_socket=gateway.open)
        members.append(DiscordPlatform(DiscordSettings(token=token, bot=name, typing_indicator=False, reply_in_thread=False), client=client))
        transports.append(rest)
        gateways.append(gateway)
    return members, transports, gateways


@pytest.fixture
async def bots():
    members, transports, gateways = _two_bots()
    port = DiscordBots(members)
    received = []

    async def on_inbound(record):
        received.append(record)
        return "answered:test"

    await port.start(on_inbound)
    await asyncio.gather(*(member._registration for member in members))
    yield port, members, transports, received, gateways
    await port.stop()


def message(channel=CHANNEL, guild=GUILD, text="hello"):
    return {"op": 0, "s": 9, "t": "MESSAGE_CREATE", "d": {
        "id": MESSAGE, "channel_id": channel, "guild_id": guild,
        "author": {"id": ALICE}, "content": text, "type": 0,
    }}


async def test_same_message_and_dm_ids_remain_distinct_and_rest_ids_stay_raw(bots):
    port, members, rests, received, _ = bots
    for member in members:
        await member.handle(message())
        await member.handle(message(DM, ""))
    assert [record.workspace for record in received] == [f"east:{GUILD}", "east:direct", f"west:{GUILD}", "west:direct"]
    assert all(record.message_id == MESSAGE for record in received)
    assert port.request_metadata(received[0])["discord_guild_id"] == GUILD
    assert port.request_metadata(received[1])["discord_guild_id"] == ""
    assert port.request_metadata(received[2])["discord_bot"] == "west"
    members[0]._learn_message(MESSAGE, CHANNEL, "east only")
    assert members[1]._messages[MESSAGE][1] == "hello"
    await port.post(CHANNEL, "", "west reply", workspace=f"west:{GUILD}")
    assert not rests[0].posted and rests[1].posted[-1][1]["content"] == "west reply"
    with pytest.raises(InstallationUnknown):
        await port.post(CHANNEL, "", "refused", workspace=f"missing:{GUILD}")
    with pytest.raises(InstallationUnknown):
        await port.post(CHANNEL, "", "refused", workspace=GUILD)


async def test_each_bot_registers_commands_and_owns_question_controls(bots):
    port, members, rests, received, _ = bots
    for index, member in enumerate(members):
        assert rests[index].calls("POST", f"/applications/{member.gateway.application_id}/commands")
        await port.ask(CHANNEL, "", Question("same-question", "Proceed?", ("yes", "no")), workspace=f"{member.bot}:{GUILD}")
    custom_ids = [rest.posted[-1][1]["components"][0]["components"][0]["custom_id"] for rest in rests]
    assert custom_ids[0] != custom_ids[1]
    interaction = {"id": MESSAGE, "type": 3, "token": "synthetic-interaction", "guild_id": GUILD,
        "channel_id": CHANNEL, "member": {"user": {"id": ALICE}}, "data": {"custom_id": custom_ids[0]}}
    assert await members[1].questions.on_interaction(interaction) == "ignored:button-of-another-start"
    assert not received
    assert await members[0].questions.on_interaction(interaction) == "answered:test"
    assert received[-1].workspace == f"east:{GUILD}"
    assert len(members[1].questions._shown) == 1


async def test_tools_choose_metadata_bot_and_never_fall_back(bots):
    port, members, rests, received, _ = bots
    await members[1].handle(message(DM, ""))
    metadata = port.request_metadata(received[-1])
    toolkit = port.toolkits(lambda: metadata)[0]
    assert json.loads(await toolkit.post_message("hello west"))["ok"]
    assert not rests[0].posted and rests[1].posted[-1][0] == DM
    metadata["discord_bot"] = "unknown"
    assert json.loads(await toolkit.post_message("must not post"))["error"] == "discord_bot_unknown"
    assert len(rests[1].posted) == 1


async def test_post_message_tool_routes_same_guild_and_channel_to_each_bot(bots):
    port, members, rests, received, _ = bots
    for member in members:
        await member.handle(message())
    host = ToolHost([])
    host.serve("discord", port.toolkits(request_metadata))
    expected = [[], []]
    for index in (0, 1, 0):
        metadata = port.request_metadata(received[index])
        assert metadata["discord_bot"] == members[index].bot
        assert metadata["discord_guild_id"] == GUILD
        assert metadata["discord_channel_id"] == CHANNEL
        turn = BoundTurn(f"request-{index}-{len(expected[index])}", platform="discord", metadata=metadata)
        host.bind("conversation", turn)
        text = f"hello {members[index].bot} {len(expected[index])}"
        result = json.loads(await host.call(ToolCall("conversation", "post_message", {"text": text})))
        assert result["ok"]
        expected[index].append((CHANNEL, text))
        assert [[(channel, payload["content"]) for channel, payload, _message_id in rest.posted] for rest in rests] == expected
        host.release("conversation", turn.request_id)


async def test_terminal_loss_of_one_bot_does_not_stop_the_other(bots):
    port, members, rests, received, gateways = bots
    waiting = asyncio.create_task(port.wait_down())
    gateways[0].sockets[0].end(4014)
    await asyncio.wait_for(members[0].wait_down(), 2)
    assert not waiting.done()
    assert port.health["east"] != "up" and port.health["west"] == "up"
    await members[1].handle(message())
    await port.post(CHANNEL, "", "still serving", workspace=f"west:{GUILD}")
    assert received[-1].workspace == f"west:{GUILD}" and rests[1].posted
    waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)


async def test_separate_guilds_commands_and_scheduled_metadata_keep_bot_identity(bots, monkeypatch):
    port, members, rests, received, _ = bots
    second_guild = "1000000000000000201"
    members[1]._channels.clear()
    rests[1].channels[CHANNEL]["guild_id"] = second_guild
    await members[0].handle(message())
    await members[1].handle(message(guild=second_guild))
    assert [record.workspace for record in received] == [f"east:{GUILD}", f"west:{second_guild}"]

    class Host:
        def admits(self, user):
            return True

        def serves(self, channel):
            return True

    seen = []

    async def answer(host, receiver, at, text, command):
        seen.append(at)
        return "ok", "answered:test"

    for member, guild in zip(members, (GUILD, second_guild), strict=True):
        member.bind(Host())
        monkeypatch.setattr(member.commands, "answer", answer)
        await member.commands._command({"channel_id": CHANNEL, "guild_id": guild,
            "member": {"user": {"id": ALICE}}}, "status")
        await member.commands._command({"channel_id": DM, "user": {"id": ALICE}}, "status")
    assert [at.workspace for at in seen] == [f"east:{GUILD}", "east:direct", f"west:{second_guild}", "west:direct"]
    metadata = port.request_metadata(received[-1])
    metadata["discord_user_id"] = ""  # Scheduled results retain installation metadata without a requester.
    result = await port.toolkits(lambda: metadata)[0].post_message("scheduled result")
    assert json.loads(result)["ok"] and not rests[0].posted
    assert rests[1].posted[-1][1]["content"] == "scheduled result"


async def test_approval_and_stop_clicks_keep_the_originating_bot(bots):
    from belle_agent.backend import APPROVAL

    port, members, rests, received, _ = bots
    for member in members:
        await port.ask(CHANNEL, "", Question("approval", "Allow?", ("approve", "deny"), kind=APPROVAL),
                       workspace=f"{member.bot}:{GUILD}")
    button = rests[1].posted[-1][1]["components"][0]["components"][0]["custom_id"]
    interaction = {"id": MESSAGE, "type": 3, "token": "synthetic-interaction", "guild_id": GUILD,
        "channel_id": CHANNEL, "member": {"user": {"id": ALICE}}, "data": {"custom_id": button}}
    await members[1].questions.on_interaction(interaction)
    assert received[-1].workspace == f"west:{GUILD}" and received[-1].answer.approved
    stop = members[1].questions.stop_button("Stop", message_id=MESSAGE)
    interaction["data"]["custom_id"] = stop["custom_id"]
    await members[1].questions.on_interaction(interaction)
    assert received[-1].workspace == f"west:{GUILD}" and received[-1].message_id == MESSAGE


async def test_start_failure_leaves_a_healthy_bot_serving(bots, monkeypatch):
    from belle_agent.config import BelleConfigError

    port, members, rests, _received, _ = bots
    metadata = {"discord_bot": "east"}
    tools = port.toolkits(lambda: metadata)[0]
    await port.stop()

    async def failed(receiver):
        raise BelleConfigError("synthetic failure")

    async def started(receiver):
        members[1]._on_inbound = receiver

    monkeypatch.setattr(members[0], "start", failed)
    monkeypatch.setattr(members[1], "start", started)
    await port.start(lambda record: None)
    assert list(port.serving) == ["west"]
    assert port.health == {"east": "not served", "west": "up"}
    with pytest.raises(InstallationUnknown):
        await port.post(CHANNEL, "", "refused", workspace=f"east:{GUILD}")
    refused = json.loads(await tools.post_message("must not post"))
    assert refused["error"] == "discord_bot_unknown"
    assert not rests[0].posted


async def test_withdrawal_routes_only_to_the_question_workspace(bots, monkeypatch):
    port, members, _rests, _received, _ = bots
    seen = []

    async def withdraw(question_id, *, workspace):
        seen.append((question_id, workspace))

    monkeypatch.setattr(members[1], "withdraw_question", withdraw, raising=False)
    await port.withdraw_question("question", workspace=f"west:{GUILD}")
    assert seen == [("question", f"west:{GUILD}")]
    with pytest.raises(InstallationUnknown):
        await port.withdraw_question("question", workspace=GUILD)


@pytest.mark.parametrize("tool_name", ["delete_message", "end_discord_poll"])
async def test_named_bot_approval_cannot_run_after_its_turn_ends(bots, monkeypatch, tool_name):
    port, members, _, received, _ = bots
    await members[1].handle(message())
    held, executed = [], []

    async def ask(name, arguments):
        return "Approve this change?"

    async def execute(message_id, chat_id=None):
        executed.append(message_id)
        return json.dumps({"ok": True})

    async def confirm(call, turn, question):
        held.append((call, turn))
        return True

    for member in members:
        kits = member.toolkits(request_metadata)
        for kit in kits:
            if tool_name in {definition["name"] for definition in kit.get_tool_definitions()}:
                monkeypatch.setattr(kit, "widening_question", ask)
                monkeypatch.setattr(kit, tool_name, execute)
        monkeypatch.setattr(member, "toolkits", lambda provider, kits=kits: kits)
    host = ToolHost([], confirm=confirm)
    host.serve("discord", port.toolkits(request_metadata))
    turn = BoundTurn("request", platform="discord", metadata=port.request_metadata(received[-1]))
    host.bind("conversation", turn)
    call = ToolCall("conversation", tool_name, {"message_id": MESSAGE})
    assert json.loads(await host.call(call))["error"] == "awaiting_confirmation"
    assert held == [(call, turn)]
    host.release("conversation", turn.request_id)
    assert json.loads(await host.run_confirmed(*held[0]))["error"] == "turn_ended"
    assert executed == []


async def test_a_duplicate_identity_cannot_read_events_or_register_commands():
    members, rests, gateways = [], [], []
    for name in ("east", "duplicate"):
        rest = FakeRest(serve=True)
        rest.gateway()

        def opens(socket):
            socket.feed(hello())
            socket.feed(ready())
            socket.feed(message(DM, ""))

        gateway = FakeGateway(opens)
        client = DiscordClient("synthetic", transport=rest.transport(), open_socket=gateway.open)
        members.append(DiscordPlatform(DiscordSettings(token="synthetic", bot=name, typing_indicator=False), client=client))
        rests.append(rest)
        gateways.append(gateway)
    port = DiscordBots(members)
    received = []

    async def on_inbound(record):
        received.append(record)
        return "answered:test"

    try:
        await port.start(on_inbound)
        await asyncio.sleep(0)
        assert list(port.serving) == ["east"]
        assert all(record.workspace.startswith("east:") for record in received)
        assert not rests[1].calls("POST", f"/applications/{members[1].gateway.application_id}/commands")
        assert members[1].receiver is None and members[1]._registration is None
        assert gateways[1].sockets[0].closed_with == 1000
    finally:
        await port.stop()


async def _until(condition) -> None:
    async def poll():
        while not condition():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), 5)


@pytest.mark.parametrize("silent", ["rest", "gateway"])
async def test_a_bot_discord_does_not_answer_is_connecting_then_served(monkeypatch, silent):
    monkeypatch.setattr(installations, "RETRY_FIRST_SECONDS", 0.01)
    monkeypatch.setattr(installations, "RETRY_MAX_SECONDS", 0.01)
    members, rests, gateways = _two_bots()
    if silent == "rest":
        rests[1].unreachable = True
    else:
        gateways[1].refuse = 3
    port = DiscordBots(members)

    async def on_inbound(record):
        return "answered:test"

    await port.start(on_inbound)
    try:
        assert port.health == {"east": "up", "west": "connecting"}
        with pytest.raises(InstallationConnecting, match="bot west has not connected yet"):
            await port.post(CHANNEL, "", "too early", workspace=f"west:{GUILD}")

        rests[1].unreachable = False
        await _until(lambda: "west" in port.serving)

        assert port.health == {"east": "up", "west": "up"}
        assert len(gateways[1].sockets) == 1
        await port.post(CHANNEL, "", "now", workspace=f"west:{GUILD}")
        assert rests[1].posted[-1][1]["content"] == "now"
    finally:
        await port.stop()
