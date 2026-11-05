"""Slack workspace settings retain their gateway routing keys."""

import asyncio

import pytest

from belle_agent.config import BelleConfigError
from belle_agent.integrations.jiuwenswarm import slack_gateway


def test_workspaces_keep_indices_and_resolve_per_workspace_delivery(monkeypatch) -> None:
    checked = []

    async def check_delivery(settings, **kwargs):
        checked.append((settings.app_id, settings.default_channel_id, kwargs))

    monkeypatch.setattr(slack_gateway, "load_slack_scopes", lambda: ())
    monkeypatch.setattr(slack_gateway, "load_brain_ingest_channels", lambda: (False, False, frozenset()))
    monkeypatch.setattr(slack_gateway, "validate_belle_slack_startup", lambda _: None)
    monkeypatch.setattr(slack_gateway, "describe_configured_channels", lambda _: None)
    monkeypatch.setattr(slack_gateway, "describe_post_tool_reachability", lambda _: None)
    monkeypatch.setattr(slack_gateway, "describe_slack_delivery_reachability", check_delivery)

    config = {
        "workspaces": [
            {"bot_token": " one ", "app_token": " first ", "default_channel_id": " C1 "},
            {"bot_token": "", "app_token": "skip"},
            {"bot_token": " three ", "app_token": " third ", "default_channel_id": " C3 "},
        ],
        "group_digital_avatar": True,
        "my_user_id": " U1 ",
        "activity_card_delay_seconds": 3,
        "optional_reply_mode": "post_tool",
        "decision_forward": True,
        "enable_streaming": True,
    }
    shared, workspaces = asyncio.run(
        slack_gateway.build_slack_workspaces(
            config,
            is_enabled=lambda item: (bool(item.get("bot_token")), "missing bot_token"),
            cron_store="store",
            heartbeat_target="slack",
        )
    )

    assert shared.my_user_id == "U1"
    assert shared.activity_card_delay_seconds == 3
    assert shared.optional_reply_mode == "post_tool"
    assert shared.decision_forward is True
    assert shared.enable_streaming is True
    assert [(index, settings.app_id, settings.bot_token) for index, settings in workspaces] == [
        (0, "workspace-1", "one"),
        (2, "workspace-3", "three"),
    ]
    assert checked == [
        ("workspace-1", "C1", {"cron_store": "store", "heartbeat_target": "slack"}),
        ("workspace-3", "C3", {"cron_store": "store", "heartbeat_target": "slack"}),
    ]


def test_disabled_and_single_workspace_settings(monkeypatch) -> None:
    monkeypatch.setattr(slack_gateway, "load_slack_scopes", lambda: ())
    monkeypatch.setattr(slack_gateway, "load_brain_ingest_channels", lambda: (False, False, frozenset()))
    monkeypatch.setattr(slack_gateway, "validate_belle_slack_startup", lambda _: None)
    monkeypatch.setattr(slack_gateway, "describe_configured_channels", lambda _: None)
    monkeypatch.setattr(slack_gateway, "describe_post_tool_reachability", lambda _: None)

    async def check_delivery(*args, **kwargs):
        pass

    monkeypatch.setattr(slack_gateway, "describe_slack_delivery_reachability", check_delivery)
    settings = {"workspaces": [{"bot_token": "one", "app_token": "first"}]}
    disabled = asyncio.run(
        slack_gateway.build_slack_workspaces(
            settings | {"enabled": False},
            is_enabled=lambda _: (True, ""),
            cron_store=None,
            heartbeat_target="web",
        )
    )
    assert disabled == (None, [])

    _, workspaces = asyncio.run(
        slack_gateway.build_slack_workspaces(
            settings,
            is_enabled=lambda _: (True, ""),
            cron_store=None,
            heartbeat_target="web",
        )
    )
    assert len(workspaces) == 1
    assert workspaces[0][1].app_id == ""

    settings["workspaces"][0]["bot_token"] = "new"
    _, rebuilt = asyncio.run(
        slack_gateway.build_slack_workspaces(
            settings,
            is_enabled=lambda _: (True, ""),
            cron_store=None,
            heartbeat_target="web",
        )
    )
    assert workspaces[0][1].bot_token == "one"
    assert rebuilt[0][1].bot_token == "new"


@pytest.mark.parametrize("value", ["5", -1, True, [1]])
def test_a_card_setting_that_is_not_seconds_is_refused(monkeypatch, value) -> None:
    monkeypatch.setattr(slack_gateway, "load_slack_scopes", lambda: ())
    monkeypatch.setattr(slack_gateway, "load_brain_ingest_channels", lambda: (False, False, frozenset()))
    monkeypatch.setattr(slack_gateway, "validate_belle_slack_startup", lambda _: None)
    config = {"workspaces": [{"bot_token": "one", "app_token": "first"}], "activity_card_delay_seconds": value}

    with pytest.raises(BelleConfigError) as caught:
        slack_gateway.configure_slack_workspaces(config, is_enabled=lambda _: (True, ""))

    assert str(caught.value) == "Belle config platforms.slack.activity_card_delay_seconds must be a number of seconds, 0 or more"
