"""Named Discord bots admit messages while command registration is pending."""

import asyncio
import sys
from pathlib import Path

import httpx
from discord_transport import TOKEN, FakeGateway, FakeRest, hello, ready

sys.path.append(str(Path(__file__).resolve().parents[1] / "conformance"))

from conformance_kit import RecordedBackend
from scripted_backend import ScriptedBackend

from belle_agent.admission.dedup import EventDedupStore
from belle_agent.host import ConversationHost, HostedPlatform
from belle_agent.platforms.discord.bots import DiscordBots
from belle_agent.platforms.discord.client import DiscordClient
from belle_agent.platforms.discord.connector import DiscordPlatform
from belle_agent.platforms.discord.settings import DiscordSettings

DM = "1000000000000000500"
ALICE = "1000000000000000700"
MESSAGE = "1000000000000000800"


async def test_a_message_during_registration_runs_once_on_its_named_bot(tmp_path, monkeypatch):
    entered, release, admitted = (asyncio.Event() for _ in range(3))
    rest = FakeRest(serve=True)
    rest.gateway()
    rest.channels[DM] = {"id": DM, "type": 1}

    async def answer(request):
        if request.url.path.endswith("/commands"):
            entered.set()
            await release.wait()
        return rest._answer(request)

    def opens(socket):
        socket.feed(hello())
        socket.feed(ready())

    gateway = FakeGateway(opens)
    client = DiscordClient(TOKEN, transport=httpx.MockTransport(answer), open_socket=gateway.open)
    platform = DiscordPlatform(DiscordSettings(token=TOKEN, bot="east", typing_indicator=False), client=client)
    bots = DiscordBots([platform])
    backend = RecordedBackend(ScriptedBackend())
    host = ConversationHost([HostedPlatform(bots, EventDedupStore(tmp_path / "seen.json"))], backend)
    receive, outcomes = host.receive, []

    async def receive_and_record(record):
        outcomes.append(await receive(record))
        if len(outcomes) == 2:
            admitted.set()
        return outcomes[-1]

    monkeypatch.setattr(host, "receive", receive_and_record)
    starting = asyncio.create_task(host.start())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        socket = gateway.sockets[0]
        platform.gateway._acked = False
        socket.feed({"op": 11, "d": None})
        event = {"op": 0, "s": 9, "t": "MESSAGE_CREATE", "d": {
            "id": MESSAGE, "channel_id": DM, "author": {"id": ALICE}, "content": "hello", "type": 0,
        }}
        socket.feed(event)
        socket.feed(event)
        await asyncio.wait_for(admitted.wait(), 1)
        await asyncio.wait_for(host.idle(), 1)

        assert not release.is_set() and platform.gateway._acked
        assert len(backend.requests) == 1
        assert backend.requests[0].metadata["discord_bot"] == "east"
        assert [(channel, body["content"]) for channel, body, _ in rest.posted] == [(DM, "ok")]
        assert outcomes[1] == "ignored:already-handled (dedupe)"
    finally:
        release.set()
        await starting
        await host.stop()
