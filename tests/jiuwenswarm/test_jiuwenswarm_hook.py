"""When the second-brain ingest hook may be installed at all.

Installing it is what makes the connector's silent branch live, and that branch
*claims* a message -- the agent never sees it. So a hook installed where the
feature is off does not merely do nothing: it takes every link and every
attachment out of a conversation that was working before.
"""

from types import SimpleNamespace

import pytest

from belle_agent.integrations.jiuwenswarm import second_brain_gateway
from belle_agent.integrations.jiuwenswarm.slack_channel import SlackChannel
from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager


class _Channel:
    channel_id = "slack"
    bind_agent_client = SlackChannel.bind_agent_client

    def __init__(self) -> None:
        self.hook = None

    def set_brain_ingest_hook(self, hook) -> None:
        self.hook = hook


def _manager(monkeypatch, *, enabled: bool, shared: bool = True) -> ChannelManager:
    manager = ChannelManager.__new__(ChannelManager)
    manager._message_handler = SimpleNamespace(agent_client=object())
    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.e2a_proxy.is_legacy_shared_directory_client",
        lambda client: shared,
    )
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: {})
    monkeypatch.setattr(second_brain_gateway, "second_brain_config", lambda _: {"enabled": enabled})
    return manager


def test_no_hook_when_the_second_brain_is_disabled(monkeypatch) -> None:
    channel = _Channel()
    _manager(monkeypatch, enabled=False)._try_bind_agent_client(channel)
    assert channel.hook is None


def test_hook_when_enabled_and_the_filesystem_is_shared(monkeypatch) -> None:
    channel = _Channel()
    _manager(monkeypatch, enabled=True)._try_bind_agent_client(channel)
    assert channel.hook is not None


def test_no_hook_when_the_runtime_is_remote(monkeypatch) -> None:
    """A path we wrote is meaningless to a runtime with a different filesystem."""
    channel = _Channel()
    _manager(monkeypatch, enabled=True, shared=False)._try_bind_agent_client(channel)
    assert channel.hook is None


def test_no_hook_without_an_agent_client(monkeypatch) -> None:
    channel = _Channel()
    manager = _manager(monkeypatch, enabled=True)
    manager._message_handler = SimpleNamespace(agent_client=None)
    manager._try_bind_agent_client(channel)
    assert channel.hook is None


def test_a_channel_that_cannot_ingest_is_skipped(monkeypatch) -> None:
    manager = _manager(monkeypatch, enabled=True)
    manager._try_bind_agent_client(SimpleNamespace(channel_id="telegram"))


@pytest.mark.asyncio
async def test_hook_forwards_brain_ingest_and_raises_rpc_errors(monkeypatch) -> None:
    channel = _Channel()
    manager = _manager(monkeypatch, enabled=True)
    calls = []

    async def fetch(**kwargs):
        calls.append(kwargs)
        return (True, {"status": "ingested"}) if len(calls) == 1 else (False, {"error": "offline"})

    monkeypatch.setattr("jiuwenswarm.gateway.routing.e2a_proxy.fetch_agent_unary", fetch)
    manager._try_bind_agent_client(channel)

    assert await channel.hook({"kind": "link", "url": "https://example.com"}) == {
        "status": "ingested"
    }
    assert calls[0] == {
        "agent_client": manager._message_handler.agent_client,
        "req_method": "brain.ingest",
        "params": {"kind": "link", "url": "https://example.com"},
        "session_id": None,
        "user_id": None,
        "channel_id": "slack",
        "label": "brain.ingest",
    }
    with pytest.raises(RuntimeError, match="offline"):
        await channel.hook({"kind": "link"})
