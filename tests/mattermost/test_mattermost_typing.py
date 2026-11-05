"""The bot shown typing while the turn of a message runs, and only then."""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
from transport import TOKEN, URL, EventFeeder, FakeRest, hello

from belle_agent.platforms import base
from belle_agent.platforms.base import InboundMessage
from belle_agent.platforms.mattermost import typing_indicator
from belle_agent.platforms.mattermost.client import MattermostClient
from belle_agent.platforms.mattermost.connector import MattermostPlatform
from belle_agent.platforms.mattermost.settings import MattermostSettings

CHANNEL = "c" * 26
POST = "p" * 26
ROOT = "r" * 26
MESSAGE = InboundMessage(
    platform="mattermost", workspace="t" * 26, conversation=CHANNEL, message_id=POST, sender="a" * 26, text="hi",
    thread=ROOT, reply_thread=ROOT,
)
#: The server's interval, in milliseconds: a notice is sent every 50 ms.
INTERVAL = "100"
Server = tuple[MattermostPlatform, FakeRest]


async def _ignore(_record: Any) -> str:
    return "ignored"


async def _connector(rest: FakeRest, **settings: Any) -> MattermostPlatform:
    client = MattermostClient(URL, TOKEN, transport=rest.transport(), open_events=EventFeeder(hello()).open)
    connector = MattermostPlatform(MattermostSettings(URL, TOKEN, **settings), client=client)
    await connector.start(_ignore)
    return connector


@pytest.fixture
async def server() -> AsyncIterator[Server]:
    rest = FakeRest()
    rest.client_config["TimeBetweenUserTypingUpdatesMilliseconds"] = INTERVAL
    connector = await _connector(rest)
    yield connector, rest
    await connector.stop()


class _Kept(logging.Handler):
    """Keep each record logged."""

    def __init__(self, level: int) -> None:
        super().__init__(level)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _notices(rest: FakeRest) -> list[Any]:
    return [request.body for request in rest.calls("POST", "/users/me/typing")]


@pytest.mark.parametrize("start", [base.MARK_RECEIVED, base.MARK_STARTED])
async def test_a_running_turn_shows_the_bot_typing_where_its_answer_goes_again_and_again(server: Server, start: str) -> None:
    connector, rest = server

    await connector.mark(MESSAGE, start)
    await asyncio.sleep(0.18)

    notices = _notices(rest)
    assert len(notices) >= 3
    assert set(map(str, notices)) == {str({"channel_id": CHANNEL, "parent_id": ROOT})}


@pytest.mark.parametrize("end", [base.MARK_DONE, base.MARK_FAILED, base.MARK_STOPPED, base.MARK_DROPPED])
async def test_each_end_of_the_turn_stops_the_notices(server: Server, end: str) -> None:
    connector, rest = server
    await connector.mark(MESSAGE, base.MARK_RECEIVED)
    await asyncio.sleep(0.07)

    await connector.mark(MESSAGE, end)
    sent = len(_notices(rest))
    await asyncio.sleep(0.15)

    assert sent >= 1 and len(_notices(rest)) == sent


@pytest.mark.parametrize("state", [base.MARK_QUEUED, base.MARK_STEERED, base.MARK_REFUSED])
async def test_a_message_whose_own_turn_does_not_run_shows_no_typing(server: Server, state: str) -> None:
    connector, rest = server

    await connector.mark(MESSAGE, state)
    await asyncio.sleep(0.07)

    assert _notices(rest) == []


async def test_a_direct_message_shows_typing_below_its_channel(server: Server) -> None:
    connector, rest = server
    direct = InboundMessage(platform="mattermost", workspace="direct", conversation=CHANNEL, message_id=POST, sender="a" * 26, text="hi")

    await connector.mark(direct, base.MARK_RECEIVED)
    await asyncio.sleep(0.02)
    await connector.mark(direct, base.MARK_DONE)

    assert _notices(rest)[0] == {"channel_id": CHANNEL, "parent_id": ""}


async def test_stopping_the_connector_stops_the_notices(server: Server) -> None:
    connector, rest = server
    await connector.mark(MESSAGE, base.MARK_RECEIVED)

    await connector.stop()
    sent = len(_notices(rest))
    await asyncio.sleep(0.12)

    assert len(_notices(rest)) == sent


async def test_the_setting_off_sends_nothing() -> None:
    rest = FakeRest()
    connector = await _connector(rest, typing_indicator=False)
    try:
        await connector.mark(MESSAGE, base.MARK_RECEIVED)
        await asyncio.sleep(0.02)
    finally:
        await connector.stop()

    assert _notices(rest) == []


async def test_typing_messages_off_on_the_server_send_nothing_and_say_so_once(server: Server) -> None:
    connector, rest = server
    rest.client_config["EnableUserTypingMessages"] = "false"
    handler = _Kept(logging.INFO)
    typing_indicator.logger.addHandler(handler)
    typing_indicator.logger.setLevel(logging.INFO)
    try:
        for message_id in ("m" * 26, "n" * 26):
            await connector.mark(InboundMessage(
                platform="mattermost", workspace="t" * 26, conversation=CHANNEL, message_id=message_id, sender="a" * 26, text="hi",
            ), base.MARK_RECEIVED)
        await asyncio.sleep(0.02)
    finally:
        typing_indicator.logger.removeHandler(handler)

    assert _notices(rest) == []
    assert [record.getMessage() for record in handler.records] == [
        "mattermost typing indicator off: the server sets ServiceSettings.EnableUserTypingMessages to false"
    ]


async def test_a_refused_notice_stops_the_notices_and_the_message_is_still_marked(server: Server) -> None:
    connector, rest = server
    rest.routes[("POST", "/users/me/typing")] = (403, {"id": "api.context.permissions.app_error", "status_code": 403})

    await connector.mark(MESSAGE, base.MARK_RECEIVED)
    await asyncio.sleep(0.12)

    assert len(_notices(rest)) == 1
    assert rest.calls("POST", "/reactions")[0].body["emoji_name"] == "eyes"


async def test_unreadable_client_settings_send_nothing_and_the_message_is_still_marked(server: Server) -> None:
    connector, rest = server
    connector.directory._client_config = None
    rest.routes[("GET", "/config/client")] = (500, {"id": "app_error", "status_code": 500})

    await connector.mark(MESSAGE, base.MARK_RECEIVED)
    await asyncio.sleep(0.02)

    assert _notices(rest) == []
    assert rest.calls("POST", "/reactions")[0].body["emoji_name"] == "eyes"
