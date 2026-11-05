"""The gateway: identify with the intents of the settings, heartbeat, a dead connection, resume, reconnect,
invalid sessions, and the close codes that stop it with what to fix."""

import asyncio
from types import SimpleNamespace

import pytest
from discord_transport import GATEWAY_URL, RESUME_URL, TOKEN, FakeGateway, FakeRest, dispatch, hello, ready

from belle_agent.platforms.discord.client import DiscordClient, _AiohttpSocket
from belle_agent.platforms.discord.gateway import (
    BASE_INTENTS,
    FATAL_CLOSES,
    INTENT_GUILD_MEMBERS,
    INTENT_MESSAGE_CONTENT,
    DiscordGateway,
    GatewayStopped,
)
from belle_agent.platforms.discord.settings import DiscordSettings

#: A heartbeat interval that the scaled sleep turns into 30 ms.
INTERVAL_MS = 30_000


class Waits:
    """Records each wait the gateway asks for, and waits a thousandth of it."""

    def __init__(self) -> None:
        self.asked: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.asked.append(round(seconds, 3))
        await asyncio.sleep(seconds / 1000)


def acks(socket):
    """A socket whose server answers each heartbeat with an ACK."""
    original = socket.send

    async def send(payload):
        await original(payload)
        if payload["op"] == 1:
            socket.feed({"op": 11, "d": None, "s": None, "t": None})

    socket.send = send


def opens_with(*payloads, ack=True):
    def script(socket):
        if ack:
            acks(socket)
        socket.feed(hello(INTERVAL_MS))
        for payload in payloads:
            socket.feed(payload)
    return script


def _gateway(gateway, settings=None, *, rest=None, waits=None):
    rest = rest or FakeRest()
    if ("GET", "/gateway/bot") not in rest.routes:
        rest.gateway()
    client = DiscordClient(TOKEN, transport=rest.transport(), open_socket=gateway.open)
    return DiscordGateway(client, settings or DiscordSettings(token=TOKEN), sleep=waits or Waits(), jitter=lambda: 0.5)


async def _take(events, count, seconds=2.0):
    found = []
    async with asyncio.timeout(seconds):
        async for event in events:
            found.append(event)
            if len(found) == count:
                return found
    return found


async def test_identify_sends_the_token_and_the_intents_and_ready_names_the_session():
    gateway = FakeGateway(opens_with(ready(guilds=3)))
    discord = _gateway(gateway)

    data = await discord.connect()

    (socket,) = gateway.sockets
    identify = socket.sent[0]
    assert socket.url == f"{GATEWAY_URL}/?v=10&encoding=json" and identify["op"] == 2
    assert identify["d"]["token"] == TOKEN and identify["d"]["intents"] == BASE_INTENTS | INTENT_MESSAGE_CONTENT
    assert (data["session_id"], discord.user["username"], len(discord.guilds)) == ("session-1", "belle", 3)
    assert discord.resume_url == f"{RESUME_URL}/?v=10&encoding=json"
    await discord.close()


async def test_ready_names_the_application_that_answers_interactions():
    gateway = FakeGateway(opens_with(ready()))
    discord = _gateway(gateway)

    await discord.connect()

    assert discord.application_id == "1000000000000000116"
    await discord.close()


@pytest.mark.parametrize(
    ("message_content", "guild_members", "expected"),
    [(False, False, BASE_INTENTS), (True, True, BASE_INTENTS | INTENT_MESSAGE_CONTENT | INTENT_GUILD_MEMBERS)],
)
async def test_the_privileged_intents_follow_the_settings(message_content, guild_members, expected):
    gateway = FakeGateway(opens_with(ready()))
    discord = _gateway(gateway, DiscordSettings(token=TOKEN, message_content=message_content, guild_members=guild_members))

    await discord.connect()

    assert gateway.sockets[0].sent[0]["d"]["intents"] == expected
    await discord.close()


async def test_heartbeats_carry_the_last_sequence_and_a_heartbeat_request_is_answered_at_once():
    waits = Waits()
    gateway = FakeGateway(opens_with(ready()))
    discord = _gateway(gateway, waits=waits)
    await discord.connect()
    socket = gateway.sockets[0]
    socket.feed(dispatch(5))
    socket.feed({"op": 1, "d": None, "s": None, "t": None})
    reader = asyncio.create_task(_take(discord.events(), 10, seconds=0.15))

    with pytest.raises(TimeoutError):
        await reader

    assert waits.asked[0] == 15.0  # the first heartbeat waits half the interval: jitter 0.5
    beats = [payload for payload in socket.sent if payload["op"] == 1]
    assert len(beats) >= 3 and {beat["d"] for beat in beats} == {5} and socket.closed_with is None
    await discord.close()


async def test_a_connection_whose_heartbeat_gets_no_ack_is_closed_and_the_session_resumed():
    gateway = FakeGateway(opens_with(ready(), ack=False), opens_with(dispatch(2, "RESUMED"), dispatch(3)))
    discord = _gateway(gateway)
    await discord.connect()

    events = await _take(discord.events(), 2)

    first, second = gateway.sockets
    assert first.closed_with == 4000 and second.url == f"{RESUME_URL}/?v=10&encoding=json"
    assert second.sent[0] == {"op": 6, "d": {"token": TOKEN, "session_id": "session-1", "seq": 1}}
    assert [event["t"] for event in events] == ["RESUMED", "MESSAGE_CREATE"]
    await discord.close()


@pytest.mark.parametrize("code", [4000, 4001, 4002, 4003, 4005, 4008, 1006])
async def test_a_close_discord_marks_as_reconnectable_resumes_the_session(code):
    gateway = FakeGateway(opens_with(ready()), opens_with(dispatch(4)))
    discord = _gateway(gateway)
    await discord.connect()
    gateway.sockets[0].end(code)

    (event,) = await _take(discord.events(), 1)

    assert event["s"] == 4 and gateway.sockets[1].sent[0]["op"] == 6
    await discord.close()


async def test_a_reconnect_request_resumes_the_session():
    gateway = FakeGateway(opens_with(ready(), {"op": 7, "d": None, "s": None, "t": None}), opens_with(dispatch(2)))
    discord = _gateway(gateway)
    await discord.connect()

    await _take(discord.events(), 1)

    assert gateway.sockets[0].closed_with == 4000 and gateway.sockets[1].sent[0]["op"] == 6
    await discord.close()


async def test_an_invalid_session_that_cannot_resume_waits_then_identifies_a_new_one():
    waits = Waits()
    invalid = {"op": 9, "d": False, "s": None, "t": None}
    gateway = FakeGateway(opens_with(ready(), invalid), opens_with(ready("session-2")))
    discord = _gateway(gateway, waits=waits)
    await discord.connect()

    (event,) = await _take(discord.events(), 1)

    second = gateway.sockets[1]
    assert 3.0 in waits.asked  # one to five seconds, here halfway
    assert second.url == f"{GATEWAY_URL}/?v=10&encoding=json" and second.sent[0]["op"] == 2
    assert event["t"] == "READY" and discord.session_id == "session-2"
    await discord.close()


async def test_an_invalid_session_that_can_resume_resumes_at_once():
    gateway = FakeGateway(opens_with(ready(), {"op": 9, "d": True, "s": None, "t": None}), opens_with(dispatch(2)))
    discord = _gateway(gateway)
    await discord.connect()

    await _take(discord.events(), 1)

    assert gateway.sockets[1].sent[0]["op"] == 6
    await discord.close()


@pytest.mark.parametrize("code", sorted(FATAL_CLOSES))
async def test_a_close_discord_marks_as_final_stops_the_gateway_with_what_to_fix(code):
    gateway = FakeGateway(opens_with(ready()))
    discord = _gateway(gateway)
    await discord.connect()
    gateway.sockets[0].end(code)

    with pytest.raises(GatewayStopped) as stopped:
        await _take(discord.events(), 1)

    assert stopped.value.code == code and FATAL_CLOSES[code] in str(stopped.value) and len(gateway.sockets) == 1
    await discord.close()


async def test_a_privileged_intent_left_off_in_the_portal_stops_the_start_naming_the_toggle():
    def refuses(socket):
        socket.feed(hello(INTERVAL_MS))
        socket.end(4014)

    discord = _gateway(FakeGateway(refuses))

    with pytest.raises(GatewayStopped, match="turn on Message Content"):
        await discord.connect()
    await discord.close()


@pytest.mark.parametrize(
    ("shards", "remaining", "named"),
    [(2, 1000, "asks for 2 shards"), (1, 0, "identify budget is spent")],
)
async def test_a_bot_discord_would_shard_or_whose_identify_budget_is_spent_does_not_connect(shards, remaining, named):
    rest = FakeRest()
    rest.gateway(shards=shards, remaining=remaining, reset_after=60_000)
    gateway = FakeGateway()

    with pytest.raises(GatewayStopped, match=named):
        await _gateway(gateway, rest=rest).connect()
    assert gateway.sockets == []


async def test_a_connection_that_does_not_reopen_is_tried_again_after_longer_waits():
    waits = Waits()
    gateway = FakeGateway(opens_with(ready()), opens_with(dispatch(2)))
    discord = _gateway(gateway, waits=waits)
    await discord.connect()
    gateway.refuse = 2
    gateway.sockets[0].end(1006)

    await _take(discord.events(), 1)

    assert [wait for wait in waits.asked if wait in (1.0, 2.0)] == [1.0, 2.0]
    await discord.close()


async def test_close_ends_the_session_with_1000_and_ends_the_events():
    gateway = FakeGateway(opens_with(ready()))
    discord = _gateway(gateway)
    await discord.connect()
    reader = asyncio.create_task(_take(discord.events(), 1))
    await asyncio.sleep(0.01)

    await discord.close()

    assert await reader == [] and gateway.sockets[0].closed_with == 1000


async def test_a_dead_connection_the_bot_closes_is_logged_with_the_code_it_sent(caplog):
    def keeps_no_code(socket):
        # As the aiohttp socket does: a close the bot makes leaves no close code on the socket.
        async def close(code):
            socket.closed_with = code
            socket._inbox.put_nowait(None)

        socket.close = close
        opens_with(ready(), ack=False)(socket)

    gateway = FakeGateway(keeps_no_code, opens_with(dispatch(2)))
    discord = _gateway(gateway)
    await discord.connect()

    with caplog.at_level("WARNING", logger="belle_agent.platforms.discord.gateway"):
        await _take(discord.events(), 1)

    assert gateway.sockets[0].close_code is None and gateway.sockets[0].closed_with == 4000
    assert "discord gateway closed (code 4000, sent by Belle): resuming session" in caplog.messages
    await discord.close()


async def test_identify_refused_after_failed_resume_stops_instead_of_retrying_forever():
    invalid = {"op": 9, "d": False, "s": None, "t": None}
    gateway = FakeGateway(opens_with(ready(), invalid), opens_with(invalid))
    discord = _gateway(gateway, waits=Waits())
    await discord.connect()
    with pytest.raises(GatewayStopped, match="Identify with Invalid Session"):
        await _take(discord.events(), 1)
    await discord.close()


@pytest.mark.parametrize("code", [4007, 4009])
async def test_invalid_sequence_and_expired_session_identify_a_new_session(code):
    gateway = FakeGateway(opens_with(ready()), opens_with(ready("session-2")))
    discord = _gateway(gateway)
    await discord.connect()
    gateway.sockets[0].end(code)

    (event,) = await _take(discord.events(), 1)

    second = gateway.sockets[1]
    assert second.url == f"{GATEWAY_URL}/?v=10&encoding=json"
    assert second.sent[0]["op"] == 2 and 6 not in second.ops()
    assert event["t"] == "READY" and discord.session_id == "session-2"
    await discord.close()


@pytest.mark.parametrize("code, opcode", [(1006, 6), (4009, 2)])
async def test_transport_send_failures_retry_and_release_each_failed_reconnect(code, opcode):
    import aiohttp
    from aiohttp._websocket.writer import WebSocketWriter

    failed_heartbeats = []

    def broken_send(socket):
        socket.feed(hello(INTERVAL_MS))
        wire = aiohttp.ClientWebSocketResponse.__new__(aiohttp.ClientWebSocketResponse)
        wire._writer = WebSocketWriter(
            SimpleNamespace(_paused=False), SimpleNamespace(is_closing=lambda: True),
        )
        adapter = _AiohttpSocket(None, wire)

        async def send(payload):
            failed_heartbeats.append(discord._heartbeat)
            await adapter.send(payload)

        socket.send = send

    class RetryWaits(Waits):
        async def __call__(self, seconds):
            if seconds in (1.0, 2.0):
                assert gateway.sockets[-1].closed_with == 4000
                assert discord._socket is None and discord._heartbeat is None
            await super().__call__(seconds)

    gateway = FakeGateway(opens_with(ready()), broken_send, broken_send, opens_with(dispatch(2, "RESUMED")))
    waits = RetryWaits()
    discord = _gateway(gateway, waits=waits)
    try:
        await discord.connect()
        gateway.sockets[0].end(code)
        (event,) = await _take(discord.events(), 1)

        assert event["t"] == "RESUMED" and gateway.sockets[-1].sent[0]["op"] == opcode
        assert [wait for wait in waits.asked if wait in (1.0, 2.0)] == [1.0, 2.0]
        assert all(task.cancelled() for task in failed_heartbeats)
    finally:
        await discord.close()
        await discord._client.close()


async def test_a_fatal_close_during_reconnect_stops_without_retrying():
    def refused(socket):
        socket.end(4014)

    gateway = FakeGateway(opens_with(ready()), refused)
    discord = _gateway(gateway)
    try:
        await discord.connect()
        gateway.sockets[0].end(1006)
        with pytest.raises(GatewayStopped) as stopped:
            await _take(discord.events(), 1)
        assert stopped.value.code == 4014 and len(gateway.sockets) == 2
    finally:
        await discord.close()
        await discord._client.close()


@pytest.mark.parametrize("requested", [False, True])
@pytest.mark.parametrize("close_behavior", ["normal", "raises", "does-not-wake"])
async def test_heartbeat_send_failure_closes_the_socket_and_resumes(requested, close_behavior):
    import aiohttp
    from aiohttp._websocket.writer import WebSocketWriter

    gateway = FakeGateway(opens_with(ready()), opens_with(dispatch(2, "RESUMED")))
    discord = _gateway(gateway)
    try:
        await discord.connect()
        first = gateway.sockets[0]
        heartbeat = discord._heartbeat
        wire = aiohttp.ClientWebSocketResponse.__new__(aiohttp.ClientWebSocketResponse)
        wire._writer = WebSocketWriter(
            SimpleNamespace(_paused=False), SimpleNamespace(is_closing=lambda: True),
        )
        first.send = _AiohttpSocket(None, wire).send
        if close_behavior != "normal":
            async def close(code):
                first.closed_with = code
                if close_behavior == "raises":
                    raise ConnectionResetError

            first.close = close
        if requested:
            first.feed({"op": 1, "d": None, "s": None, "t": None})

        (event,) = await _take(discord.events(), 1)

        assert event["t"] == "RESUMED" and len(gateway.sockets) == 2
        assert first.closed_with == 4000 and gateway.sockets[1].sent[0]["op"] == 6
        await asyncio.sleep(0)
        assert heartbeat.done()
    finally:
        await discord.close()
        await discord._client.close()
