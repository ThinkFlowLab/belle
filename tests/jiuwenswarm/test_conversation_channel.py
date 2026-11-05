"""Shared Gateway routing and lifecycle behavior for conversation channels."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from belle_agent.backend import BackendError
from belle_agent.config import BelleConfigError
from belle_agent.integrations.jiuwenswarm.conversation_channel import ConversationChannel


@pytest.fixture
def channel():
    result = ConversationChannel(SimpleNamespace(route_user_message=AsyncMock()))
    result.host = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    result.name = "matrix"
    return result


async def test_route_uses_bus_until_callback_is_registered(channel):
    message = object()
    await channel._route(message)
    channel.bus.route_user_message.assert_awaited_once_with(message)
    for callback in (Mock(), AsyncMock()):
        channel.on_message(callback)
        await channel._route(message)
        callback.assert_called_once_with(message)
        if isinstance(callback, AsyncMock):
            callback.assert_awaited_once_with(message)
    channel.bus.route_user_message.assert_awaited_once()


async def test_stop_failure_releases_the_running_channel(channel):
    started = asyncio.create_task(channel.start())
    await asyncio.sleep(0)
    assert channel._running
    channel.host.stop.side_effect = RuntimeError("stop failed")
    with pytest.raises(RuntimeError, match="stop failed"):
        await channel.stop()
    await asyncio.wait_for(started, timeout=1)
    assert not channel._running


async def test_start_failure_clears_running_state(channel):
    channel.host.start.side_effect = RuntimeError("start failed")
    with pytest.raises(RuntimeError, match="start failed"):
        await channel.start()
    assert not channel._running


@pytest.mark.parametrize("error", [BelleConfigError("no account can be served"), BackendError("no answer"), OSError("port in use")])
async def test_a_start_that_names_what_to_fix_is_logged_with_its_message(channel, caplog, error):
    channel.host.start.side_effect = error
    with caplog.at_level(logging.CRITICAL), pytest.raises(type(error)):
        await channel.start()
    assert [(record.levelno, record.getMessage(), record.exc_info) for record in caplog.records] == [
        (logging.CRITICAL, f"matrix cannot start: {error}", None)
    ]


async def test_any_other_start_failure_is_logged_with_its_traceback(channel, caplog):
    channel.host.start.side_effect = KeyError("device_id")
    with caplog.at_level(logging.ERROR), pytest.raises(KeyError):
        await channel.start()
    assert [record.getMessage() for record in caplog.records] == ["matrix cannot start"]
    assert "Traceback" in caplog.text and "KeyError: 'device_id'" in caplog.text
