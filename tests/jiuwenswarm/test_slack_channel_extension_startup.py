"""JiuwenSwarm selects its built-in channel until Belle's extension loads."""

import asyncio
from pathlib import Path

from jiuwenswarm.extensions import overrides
from jiuwenswarm.extensions.channel_contributions import contributed_spec_for, unregister_channel_spec
from jiuwenswarm.extensions.loader import ExtensionLoader
from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel as BuiltinSlackChannel,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannelConfig as BuiltinSlackConfig,
)
from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework

from belle_agent.integrations.jiuwenswarm import slack_gateway
from belle_agent.integrations.jiuwenswarm.extensions.slack import extension


def test_gateway_channel_selection_with_and_without_belle(monkeypatch) -> None:
    unregister_channel_spec("slack")
    assert contributed_spec_for("slack") is None
    host = {"bot_token": "xoxb-host", "app_token": "xapp-host"}
    assert isinstance(
        BuiltinSlackChannel(BuiltinSlackConfig(**host), RobotMessageRouter()),
        BuiltinSlackChannel,
    )

    monkeypatch.setattr(overrides, "allowed_overrides", lambda: frozenset({"channels.slack"}))
    monkeypatch.setattr(slack_gateway, "load_belle_config", lambda: {"platforms": {"slack": {}}})
    monkeypatch.setattr(slack_gateway, "load_slack_scopes", lambda: ())
    monkeypatch.setattr(slack_gateway, "load_brain_ingest_channels", lambda: (False, False, frozenset()))
    try:
        registry = ExtensionRegistry(AsyncCallbackFramework(), {}, None)
        asyncio.run(ExtensionLoader(registry).load_extension(Path(extension.__file__).parent))
        contributed = contributed_spec_for("slack")
        assert contributed.factory is not None
        channels = contributed.factory(host, RobotMessageRouter())
        assert len(channels) == 1
        assert channels[0].config.bot_token == "xoxb-host"
        assert channels[0].config.app_token == "xapp-host"
        assert not isinstance(channels[0], BuiltinSlackChannel)
    finally:
        unregister_channel_spec("slack")
