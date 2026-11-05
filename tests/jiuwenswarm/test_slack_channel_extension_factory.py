"""Belle's JiuwenSwarm channel factory reads Belle and host settings."""

import pytest

from belle_agent.integrations.jiuwenswarm import slack_gateway
from belle_agent.integrations.jiuwenswarm import slack_channel
from belle_agent.integrations.jiuwenswarm import second_brain_gateway
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter


def test_belle_channel_factory_uses_belle_tokens_then_host(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(slack_gateway, "load_belle_config", lambda: {
        "platforms": {"slack": {
            "bot_token": "xoxb-belle",
            "app_token": "",
            "group_digital_avatar": True,
            "my_user_id": "U1",
        }
    }})
    monkeypatch.setattr(slack_gateway, "load_slack_scopes", lambda: ())
    monkeypatch.setattr(slack_gateway, "load_brain_ingest_channels", lambda: (False, False, frozenset()))
    monkeypatch.setattr(slack_channel, "get_slack_dedup_path", lambda: tmp_path / "dedup.sqlite")

    channels = slack_gateway.create_slack_channels(
        {"bot_token": "xoxb-host", "app_token": "xapp-host"}, RobotMessageRouter()
    )
    assert len(channels) == 1
    assert channels[0].config.bot_token == "xoxb-belle"
    assert channels[0].config.app_token == "xapp-host"
    assert channels[0].im_platform_adapter is not None


def test_channel_binds_agent_client_for_optional_ingest(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(second_brain_gateway, "wire_brain_ingest_hook", lambda *args: seen.append(args))
    channel = slack_channel.SlackChannel(
        slack_channel.SlackChannelConfig(bot_token="xoxb-test", app_token="xapp-test"),
        RobotMessageRouter(),
    )
    client = object()

    channel.bind_agent_client(client)

    assert seen == [(channel, client)]


def test_host_tokens_start_belle_without_belle_tokens(monkeypatch) -> None:
    monkeypatch.setattr(slack_gateway, "load_belle_config", lambda: {"platforms": {"slack": {}}})
    monkeypatch.setattr(slack_gateway, "load_slack_scopes", lambda: ())
    monkeypatch.setattr(slack_gateway, "load_brain_ingest_channels", lambda: (False, False, frozenset()))

    channels = slack_gateway.create_slack_channels(
        {"bot_token": "xoxb-host", "app_token": "xapp-host"}, RobotMessageRouter()
    )
    assert len(channels) == 1
    assert (channels[0].config.bot_token, channels[0].config.app_token) == (
        "xoxb-host", "xapp-host"
    )


def test_enabled_workspace_without_complete_tokens_is_skipped(monkeypatch, caplog) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.setattr(slack_gateway, "load_belle_config", lambda: {"platforms": {"slack": {}}})

    channels = slack_gateway.create_slack_channels(
        {"enabled": True, "bot_token": "xoxb-host"}, RobotMessageRouter()
    )
    assert channels == []
    assert "missing bot_token or app_token; skipping" in caplog.text


def test_multiworkspace_avatar_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(slack_gateway, "load_belle_config", lambda: {"platforms": {"slack": {
        "group_digital_avatar": True,
        "my_user_id": "U1",
        "workspaces": [
            {"bot_token": "xoxb-a", "app_token": "xapp-a"},
            {"bot_token": "xoxb-b", "app_token": "xapp-b"},
        ],
    }}})
    monkeypatch.setattr(slack_gateway, "load_slack_scopes", lambda: ())
    monkeypatch.setattr(slack_gateway, "load_brain_ingest_channels", lambda: (False, False, frozenset()))

    with pytest.raises(ValueError, match="group_digital_avatar supports one workspace"):
        slack_gateway.create_slack_channels({}, RobotMessageRouter())
