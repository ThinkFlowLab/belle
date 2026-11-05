"""The WebSocket resumes its connection after a drop, and logs and hands over a gap it cannot resume."""

import logging
import urllib.parse

import pytest
from transport import CONNECTION, TOKEN, URL, EventFeeder, FakeRest, hello

from belle_agent.platforms.mattermost import socket as socket_module
from belle_agent.platforms.mattermost.client import MattermostClient, MattermostError
from belle_agent.platforms.mattermost.socket import MattermostSocket, NoHello

OTHER = "n3wn3wn3wn3wn3wn3wn3wn3wn3w"


@pytest.fixture
def logged():
    """The socket's log lines. Belle's loggers do not propagate, so a handler is put on this one."""
    records: list[logging.LogRecord] = []
    handler = logging.Handler(logging.DEBUG)
    handler.emit = records.append
    socket_module.logger.addHandler(handler)
    previous = socket_module.logger.level
    socket_module.logger.setLevel(logging.DEBUG)
    yield records
    socket_module.logger.removeHandler(handler)
    socket_module.logger.setLevel(previous)


def _socket(feeder: EventFeeder, clock=None, slept=None, gaps=None) -> MattermostSocket:
    client = MattermostClient(URL, TOKEN, transport=FakeRest().transport(), open_events=feeder.open)

    async def sleep(seconds):
        slept.append(seconds)

    async def on_gap(start, end):
        gaps.append((start, end))

    return MattermostSocket(
        client,
        clock=clock or (lambda: 1800000000.0),
        **({} if slept is None else {"sleep": sleep}),
        **({} if gaps is None else {"on_gap": on_gap}),
    )


def _event(seq: int) -> dict:
    return {"event": "typing", "data": {}, "seq": seq}


async def _read(socket: MattermostSocket, count: int) -> list[dict]:
    seen = []
    async for event in socket.events():
        seen.append(event)
        if len(seen) == count:
            break
    return seen


def _query(url: str) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)


async def test_a_drop_resumes_the_connection_from_the_next_sequence_number(logged):
    feeder = EventFeeder(hello(), _event(1), _event(2), None, _event(3))
    gaps = []
    socket = _socket(feeder, gaps=gaps)
    await socket.connect()

    seen = await _read(socket, 3)

    assert [event["seq"] for event in seen] == [1, 2, 3]
    assert _query(feeder.opened[1][0]) == {"connection_id": [CONNECTION], "sequence_number": ["3"]}
    assert "?" not in feeder.opened[0][0]
    assert any("resumed connection" in record.getMessage() for record in logged)
    assert not any("does not send" in record.getMessage() for record in logged)
    assert gaps == []


async def test_a_new_connection_logs_the_gap_with_its_span_and_hands_the_span_over(logged):
    times = iter([1800000000.0, 1800000001.0, 1800000001.0, 1800000091.0, 1800000091.0, 1800000092.0])
    feeder = EventFeeder(hello(), _event(1), None, hello(connection=OTHER), _event(1))
    gaps = []
    socket = _socket(feeder, clock=lambda: next(times), gaps=gaps)
    await socket.connect()

    seen = await _read(socket, 2)

    assert [event["event"] for event in seen] == ["typing", "typing"]
    assert socket.connection_id == OTHER
    (gap,) = [record.getMessage() for record in logged if "does not send" in record.getMessage()]
    assert "2027-01-15T08:00:01Z to 2027-01-15T08:01:31Z (90 s)" in gap
    assert gaps == [(1800000001.0, 1800000091.0)]


async def test_a_gap_handler_that_raises_is_logged_and_the_events_go_on(logged):
    feeder = EventFeeder(hello(), None, hello(connection=OTHER), _event(1))
    client = MattermostClient(URL, TOKEN, transport=FakeRest().transport(), open_events=feeder.open)

    async def on_gap(start, end):
        raise ValueError("synthetic")

    socket = MattermostSocket(client, clock=lambda: 1800000000.0, on_gap=on_gap)
    await socket.connect()

    assert [event["seq"] for event in await _read(socket, 1)] == [1]
    assert any("was not handled" in record.getMessage() for record in logged)


async def test_a_failed_reopening_is_tried_again_after_a_longer_wait():
    slept = []
    feeder = EventFeeder(hello(), None)
    socket = _socket(feeder, slept=slept)
    await socket.connect()
    attempts = []
    opened = feeder.open

    async def flaky(url, token):
        attempts.append(url)
        if len(attempts) < 4:
            raise MattermostError("unreachable", "GET", "/api/v4/websocket")
        feeder.feed(hello())
        feeder.feed(_event(7))
        return await opened(url, token)

    socket._client._open_events = flaky

    assert [event["seq"] for event in await _read(socket, 1)] == [7]
    assert slept == [1.0, 2.0, 4.0]


async def test_a_connection_that_does_not_start_with_hello_is_refused():
    with pytest.raises(NoHello):
        await _socket(EventFeeder(_event(1))).connect()


async def test_close_ends_the_events():
    feeder = EventFeeder(hello())
    socket = _socket(feeder)
    await socket.connect()
    await socket.close()

    assert await _read(socket, 1) == []
