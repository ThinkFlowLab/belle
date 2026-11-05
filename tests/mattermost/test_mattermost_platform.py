"""The ``mattermost`` section builds the connector, which connects and disconnects."""

import asyncio

import pytest
from transport import BOT_USER, CONNECTION, TOKEN, URL, EventFeeder, FakeRest, hello

from belle_agent.config import BelleConfigError
from belle_agent.platforms.base import create_platform
from belle_agent.platforms.mattermost.capabilities import MATTERMOST_CAPABILITIES
from belle_agent.platforms.mattermost.client import MattermostClient
from belle_agent.platforms.mattermost.connector import MattermostPlatform
from belle_agent.platforms.mattermost.settings import TOKEN_ENV, MattermostSettings


def platform(rest: FakeRest | None = None, events: EventFeeder | None = None) -> MattermostPlatform:
    """A connector on the fake server, not started."""
    events = events or EventFeeder(hello())
    client = MattermostClient(URL, TOKEN, transport=(rest or FakeRest()).transport(), open_events=events.open)
    return MattermostPlatform(MattermostSettings(URL, TOKEN), client=client)


async def _nothing(_record):
    raise AssertionError("no event here holds a post")


def test_the_section_builds_the_connector_of_the_package(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "synthetic-bot-token-0123456789")
    built = create_platform({"platforms": {"mattermost": {"url": "http://mattermost:8065"}}}, "mattermost")

    assert isinstance(built, MattermostPlatform)
    assert built.capabilities is MATTERMOST_CAPABILITIES
    assert built.public_chat_types == frozenset({"O"})
    (toolkit,) = built.toolkits(dict)
    assert [card["name"] for card in toolkit.get_tool_definitions()] == [
        "post_message", "react_to_message", "read_conversation", "download_file", "edit_message", "delete_message",
            "pin_message", "send_file", "search_messages", "search_mattermost_files", "find_by_name", "read_mattermost_pinned_messages",
            "list_mattermost_groups", "list_mattermost_bookmarks", "create_mattermost_bookmark",
            "edit_mattermost_bookmark", "delete_mattermost_bookmark",
    ]


def test_a_missing_token_stops_the_build_and_names_its_variable(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    with pytest.raises(BelleConfigError, match=TOKEN_ENV):
        create_platform({"platforms": {"mattermost": {"url": "http://mattermost:8065"}}}, "mattermost")


async def test_start_checks_the_token_reads_hello_and_stop_closes():
    events = EventFeeder(hello(), {"event": "posted", "data": {}, "seq": 1})
    rest = FakeRest()
    connector = platform(rest, events)

    await connector.start(_nothing)
    events.feed({"event": "typing", "data": {}, "seq": 2})
    while connector.socket.sequence < 2:
        await asyncio.sleep(0)

    assert (connector.bot_user_id, connector.socket.connection_id, connector.socket.sequence) == (BOT_USER, CONNECTION, 2)
    assert [request.path for request in rest.requests] == ["/users/me"]
    await connector.stop()
    assert events.closed


async def test_a_rejected_token_stops_the_start_and_names_its_variable():
    rest = FakeRest(routes={("GET", "/users/me"): (401, {"id": "api.context.session_expired.app_error"})})
    events = EventFeeder(hello())

    with pytest.raises(BelleConfigError, match=f"did not accept the bot token of {TOKEN_ENV}: token_rejected"):
        await platform(rest, events).start(_nothing)
    assert not events.opened


async def test_a_connection_that_does_not_start_with_hello_is_closed():
    events = EventFeeder({"event": "posted", "data": {}, "seq": 1})

    with pytest.raises(BelleConfigError, match="did not start with hello"):
        await platform(FakeRest(), events).start(_nothing)
    assert events.closed
