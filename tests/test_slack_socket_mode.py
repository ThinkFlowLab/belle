"""Socket Mode works without a JiuwenSwarm channel."""

import asyncio
import sys
from pathlib import Path

import pytest

from belle_agent.platforms.base import PlatformUnreachable
from belle_agent.platforms.slack import socket_mode

sys.path.append(str(Path(__file__).resolve().parent / "jiuwenswarm_slack"))

from slack_fakes import FakeSlackError, FakeSlackServerError


@pytest.mark.asyncio
async def test_socket_registers_events_and_closes(monkeypatch) -> None:
    registered = []

    class App:
        def __init__(self, *, token, logger):
            assert token == "xoxb-test"
            self.client = object()

    class Handler:
        def __init__(self, app, token):
            assert token == "xapp-test"
            self.started = self.closed = False

        async def start_async(self):
            self.started = True

        async def close_async(self):
            self.closed = True

    monkeypatch.setattr(socket_mode, "AsyncSocketModeHandler", Handler)
    socket = socket_mode.SlackSocket("xoxb-test", "xapp-test", registered.append, app_factory=App)
    await socket.run()
    await socket.close()

    assert registered == [socket.app]
    assert socket.handler.started and socket.handler.closed


def _connecting(monkeypatch, issue):
    """A socket whose client asks Slack for the connection URL with ``issue``. It records what it connected to."""

    class Client:
        wss_uri = None

        async def issue_new_wss_url(self):
            return await issue()

    class Handler:
        def __init__(self, app, token):
            self.client = Client()
            self.connected_to = []

        async def connect_async(self):
            self.connected_to.append(self.client.wss_uri)

    class App:
        def __init__(self, *, token, logger):
            self.client = object()

    monkeypatch.setattr(socket_mode, "AsyncSocketModeHandler", Handler)
    return socket_mode.SlackSocket("xoxb-test", "xapp-test", lambda _: None, app_factory=App)


@pytest.mark.asyncio
async def test_socket_connects_to_the_url_slack_issued(monkeypatch) -> None:
    async def issue():
        return "wss://issued"

    socket = _connecting(monkeypatch, issue)
    await socket.connect()

    assert socket.handler.connected_to == ["wss://issued"]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["invalid_auth", "not_allowed_token_type", "token_revoked"])
async def test_socket_raises_when_slack_refuses_the_app_token(monkeypatch, code) -> None:
    # Slack's client would retry this refusal for ever, so it is never asked to connect.
    async def issue():
        raise FakeSlackError(code)

    socket = _connecting(monkeypatch, issue)
    with pytest.raises(socket_mode.SlackAppTokenRefused) as raised:
        await socket.connect()

    assert raised.value.code == code
    assert socket.handler.connected_to == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [OSError("network unreachable"), FakeSlackError("service_unavailable"), FakeSlackServerError("")]
)
async def test_socket_reports_a_slack_that_did_not_answer_and_connects_nothing(monkeypatch, failure) -> None:
    # Slack's client would retry this for ever, so the start is told and the client is never asked to connect.
    async def issue():
        raise failure

    socket = _connecting(monkeypatch, issue)
    with pytest.raises(PlatformUnreachable, match="Slack did not answer apps.connections.open"):
        await socket.connect()

    assert socket.handler.connected_to == []


@pytest.mark.asyncio
async def test_socket_reports_a_socket_that_does_not_open_in_time(monkeypatch) -> None:
    async def issue():
        return "wss://issued"

    socket = _connecting(monkeypatch, issue)
    opening = asyncio.Event()

    async def never_opens():
        # As Slack's client does where the socket does not open: it tries again, and never returns.
        opening.set()
        await asyncio.Event().wait()

    socket.handler.connect_async = never_opens
    monkeypatch.setattr(socket_mode, "OPEN_SECONDS", 0.05)

    with pytest.raises(PlatformUnreachable, match="did not open within"):
        await asyncio.wait_for(socket.connect(), 5)
    assert opening.is_set()


@pytest.mark.asyncio
async def test_socket_raises_a_failure_that_is_neither_a_refusal_nor_no_answer(monkeypatch) -> None:
    async def issue():
        raise ValueError("a fault in the client")

    socket = _connecting(monkeypatch, issue)
    with pytest.raises(ValueError, match="a fault in the client"):
        await socket.connect()


def test_socket_reports_missing_bolt(monkeypatch) -> None:
    monkeypatch.setattr(socket_mode, "AsyncSocketModeHandler", None)
    with pytest.raises(RuntimeError, match="slack-bolt"):
        socket_mode.SlackSocket("bot", "app", lambda _: None)
