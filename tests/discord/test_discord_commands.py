"""``/belle`` on Discord: registered at start, acknowledged at once, answered for its sender alone."""

import logging

import pytest
from discord_transport import TOKEN, FakeGateway, FakeRest, hello, ready

from belle_agent.platforms.base import TRIGGER_DM, InboundMessage
from belle_agent.platforms.discord.client import DiscordClient
from belle_agent.platforms.discord.commands import DEFINITION, NOT_PERMITTED, NOT_SERVED, SUBCOMMANDS
from belle_agent.platforms.discord.connector import DiscordPlatform
from belle_agent.platforms.discord.questions import EPHEMERAL, INTERACTION_COMMAND
from belle_agent.platforms.discord.settings import DiscordSettings

APP = "1000000000000000116"
GUILD = "1000000000000000200"
CHANNEL = "1000000000000000300"
DM = "1000000000000000500"
ALICE = "1000000000000000700"
INTERACTION = "1000000000000000900"
SECRET = "Y29tbWFuZC1pbnRlcmFjdGlvbi10b2tlbg"


class Host:
    """The host's command operations, as far as these commands read them."""

    def __init__(self) -> None:
        self.admitted = {ALICE}
        self.served = {CHANNEL}

    def command_unavailable(self, words):
        return ""

    def admits(self, sender):
        return sender in self.admitted

    def serves(self, conversation):
        return conversation in self.served

    def settled(self, at):
        return None


@pytest.fixture
def rest():
    found = FakeRest(serve=True)
    found.gateway()
    found.channels[CHANNEL] = {"id": CHANNEL, "type": 0, "guild_id": GUILD}
    found.answer("POST", "/users/@me/channels", (200, {"id": DM, "type": 1}))
    return found


@pytest.fixture
async def start(rest):
    started = []

    async def build(**settings):
        def opens(socket):
            socket.feed(hello())
            socket.feed(ready())

        client = DiscordClient(TOKEN, transport=rest.transport(), open_socket=FakeGateway(opens).open)
        platform = DiscordPlatform(DiscordSettings(token=TOKEN, typing_indicator=False, **settings), client=client)
        platform.received = []

        async def on_inbound(record):
            platform.received.append(record)
            return "dispatched:test"

        platform.bind(Host())
        await platform.start(on_inbound)
        await platform._registration
        started.append(platform)
        return platform

    yield build
    for platform in started:
        await platform.stop()


def _command(text=None, *, channel=CHANNEL, user=ALICE, guild=GUILD):
    data = {"name": "belle", "type": 1}
    if text is not None:
        data["options"] = [{"name": "text", "type": 3, "value": text}]
    interaction = {"id": INTERACTION, "type": INTERACTION_COMMAND, "token": SECRET, "channel_id": channel, "data": data}
    if guild:
        interaction.update(guild_id=guild, member={"user": {"id": user, "username": "alice"}})
    else:
        interaction["user"] = {"id": user, "username": "alice"}
    return interaction


def _answers(rest):
    return [request.body for request in rest.requests if request.path.startswith(f"/webhooks/{APP}/{SECRET}")]


async def test_the_command_is_registered_globally_for_servers_and_dms(start, rest):
    await start()

    (registered,) = rest.calls("POST", f"/applications/{APP}/commands")
    assert registered.body == DEFINITION
    assert registered.body["contexts"] == [0, 1]


async def test_the_free_text_help_command_runs_in_a_bot_dm(start, rest):
    rest.channels[DM] = {"id": DM, "type": 1}
    platform = await start()

    assert await platform.commands.on_interaction(_command("help", channel=DM, guild=None)) == "answered:help"
    assert _answers(rest)[0]["content"].startswith("**Belle commands**")


async def test_a_command_discord_refuses_to_register_is_logged_and_the_platform_runs_on(start, rest, caplog):
    rest.answer("POST", f"/applications/{APP}/commands", (403, {"code": 50001, "message": "Missing Access"}))

    platform = await start()

    assert platform.receiver is not None
    assert "applications.commands" in caplog.text and "not registered" in caplog.text


async def test_a_command_is_acknowledged_first_for_its_sender_alone_then_answered(start, rest):
    platform = await start()

    outcome = await platform.commands.on_interaction(_command("help"))

    acknowledged = rest.calls("POST", f"/interactions/{INTERACTION}/{SECRET}/callback")
    assert [request.body for request in acknowledged] == [{"type": 5, "data": {"flags": EPHEMERAL}}]
    (answer,) = _answers(rest)
    assert outcome == "answered:help" and answer["content"].startswith("**Belle commands**")
    assert "`/belle status`" in answer["content"] and answer["allowed_mentions"] == {"parse": []}


async def test_an_empty_command_lists_the_commands(start, rest):
    platform = await start()

    assert await platform.commands.on_interaction(_command()) == "answered:help"


async def test_free_text_help_discloses_every_shared_command_workflow(start, rest):
    platform = await start()

    assert await platform.commands.on_interaction(_command("help")) == "answered:help"
    answer = _answers(rest)[0]["content"]
    assert DEFINITION["options"][0]["name"] == "text"
    assert all(f"`/belle {' '.join(sub.words)}" in answer for sub in SUBCOMMANDS)


async def test_a_person_the_host_does_not_admit_is_told_so(start, rest):
    platform = await start()
    platform.host.admitted = set()

    outcome = await platform.commands.on_interaction(_command("status"))

    assert outcome == "refused:allow_from" and _answers(rest)[0]["content"] == NOT_PERMITTED


async def test_a_channel_belle_does_not_serve_is_named(start, rest):
    platform = await start()
    platform.host.served = set()

    outcome = await platform.commands.on_interaction(_command("status"))

    assert outcome == "refused:channel-not-served" and _answers(rest)[0]["content"] == NOT_SERVED


async def test_a_private_question_is_posted_in_the_direct_message_and_handed_over_from_there(start, rest):
    platform = await start()

    outcome = await platform.commands.on_interaction(_command("ask what is the launch site?"))

    (anchor_channel, anchor, anchor_id) = rest.posted[-1]
    (asked,) = platform.received
    assert outcome == "asked:dispatched:test" and anchor_channel == DM and "what is the launch site?" in anchor["content"]
    assert isinstance(asked, InboundMessage)
    assert (asked.conversation, asked.workspace, asked.trigger, asked.message_id) == (DM, "direct", TRIGGER_DM, anchor_id)
    assert asked.text.endswith("what is the launch site?") and CHANNEL in asked.text
    assert _answers(rest)[0]["content"] == "Answering in your direct message with Belle. Nothing is posted here."


async def test_the_gateway_answers_the_command_in_a_task_and_names_no_token(start, rest, caplog):
    caplog.set_level(logging.INFO, logger="belle_agent.platforms.discord")
    platform = await start()

    taken = await platform.handle({"op": 0, "s": 40, "t": "INTERACTION_CREATE", "d": _command("help")})
    for task in list(platform._interactions):
        await task

    assert taken == "taken:interaction" and len(_answers(rest)) == 1 and SECRET not in caplog.text


def test_a_message_link_names_its_message():
    platform = DiscordPlatform(DiscordSettings(token=TOKEN))
    link = f"https://discord.com/channels/{GUILD}/{CHANNEL}/1000000000000000801"

    assert platform.commands.message_id(link) == "1000000000000000801"
    assert platform.commands.message_id("1000000000000000802") == "1000000000000000802"
    assert platform.commands.message_id("not a message") == ""
