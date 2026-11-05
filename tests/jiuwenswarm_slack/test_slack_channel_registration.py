"""Belle's channel extension replaces the built-in Slack connector."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from jiuwenswarm.extensions import overrides
from jiuwenswarm.extensions.channel_contributions import contributed_spec_for, unregister_channel_spec
from jiuwenswarm.extensions.loader import ExtensionLoader
from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.gateway.channel_manager.base import BaseChannel
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel as BuiltinSlackChannel,
)
from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework

from belle_agent.integrations.jiuwenswarm.extensions.slack import extension
from belle_agent.integrations.jiuwenswarm.slack_channel import SlackChannel


@pytest.mark.asyncio
async def test_belle_registers_channel_through_passed_registry() -> None:
    specs = []
    await extension.register_extensions(SimpleNamespace(register_channel_spec=specs.append))
    assert len(specs) == 1
    assert specs[0].channel_id == "slack"
    assert contributed_spec_for("slack") is None


@pytest.mark.asyncio
async def test_belle_extension_replaces_builtin_only_when_loaded(monkeypatch) -> None:
    root = Path(__file__).resolve().parents[2] / "src/belle_agent/integrations/jiuwenswarm/extensions/slack"
    assert contributed_spec_for("slack") is None
    assert issubclass(SlackChannel, BaseChannel)
    assert not issubclass(SlackChannel, BuiltinSlackChannel)

    monkeypatch.setattr(overrides, "allowed_overrides", lambda: frozenset({"channels.slack"}))
    try:
        registry = ExtensionRegistry(AsyncCallbackFramework(), {}, None)
        await ExtensionLoader(registry).load_extension(root)
        assert contributed_spec_for("slack").factory is not None
        assert contributed_spec_for("slack").source == "belle-agent"
        assert contributed_spec_for("slack").check_relay is not None
    finally:
        unregister_channel_spec("slack")
    assert contributed_spec_for("slack") is None


@pytest.mark.asyncio
async def test_belle_checks_heartbeat_relay_from_contributed_spec(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(overrides, "allowed_overrides", lambda: frozenset({"channels.slack"}))
    monkeypatch.setattr(extension, "slack_default_channel_id_from_config", lambda _: "C-DEFAULT")
    monkeypatch.setattr(extension, "warn_if_heartbeat_relay_unreachable", lambda **kw: calls.append(kw))
    try:
        registry = ExtensionRegistry(AsyncCallbackFramework(), {}, None)
        await extension.register_extensions(registry)
        contributed_spec_for("slack").check_relay("slack", {})
        assert calls == [{"heartbeat_target": "slack", "default_channel_id": "C-DEFAULT"}]
    finally:
        unregister_channel_spec("slack")
