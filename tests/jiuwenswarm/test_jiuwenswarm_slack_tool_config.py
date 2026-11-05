"""Check the JiuwenSwarm and Belle Slack config boundary."""

import pytest

from belle_agent.integrations.jiuwenswarm.slack_tool_config import slack_tool_config
from belle_agent.platforms.slack.config import normalize_slack_conf, resolve_reply_in_thread


def test_combines_host_connection_and_belle_tool_settings() -> None:
    host = {"channels": {"slack": {"bot_token": "host-token", "allow_from": ["U1"]}}}
    belle = {"platforms": {"slack": {"history_max_messages": 20, "search_enabled": True, "workspaces": []}}}

    assert slack_tool_config(host, belle) == {
        "bot_token": "host-token",
        "allow_from": ["U1"],
        "history_max_messages": 20,
        "search_enabled": True,
        "workspaces": [],
    }
    assert host["channels"]["slack"]["allow_from"] == ["U1"]


def test_belle_token_takes_precedence_and_empty_uses_host() -> None:
    host = {"channels": {"slack": {"bot_token": "host-token", "app_token": "host-app"}}}
    assert slack_tool_config(host, {"platforms": {"slack": {"bot_token": "belle-token", "app_token": ""}}}) == {
        "bot_token": "belle-token", "app_token": "host-app",
    }


def test_rejects_belle_settings_in_host_config() -> None:
    with pytest.raises(ValueError, match="Move channels.slack settings to Belle platforms.slack: command_name"):
        slack_tool_config({"channels": {"slack": {"command_name": "jw"}}}, {"platforms": {"slack": {}}})


@pytest.mark.parametrize("switch", [True, None])
def test_belle_switch_of_its_slack_section_leaves_the_host_switch_alone(switch) -> None:
    host = {"channels": {"slack": {"enabled": False, "allow_from": ["U1"]}}}

    assert slack_tool_config(host, {"platforms": {"slack": {"enabled": switch, "search_enabled": True}}}) == {
        "enabled": False, "allow_from": ["U1"], "search_enabled": True,
    }


def test_belle_slack_section_turned_off_is_refused_by_the_slack_only_channel() -> None:
    with pytest.raises(ValueError, match=r"Belle platforms\.slack\.enabled is false, and the JiuwenSwarm channel serves Slack only"):
        slack_tool_config({"channels": {"slack": {"enabled": True}}}, {"platforms": {"slack": {"enabled": False}}})


def test_belle_thread_mode_overrides_host_boolean() -> None:
    host = {"channels": {"slack": {"reply_in_thread": True}}}
    assert resolve_reply_in_thread(slack_tool_config(host, {"platforms": {"slack": {}}})) == "multi"
    assert resolve_reply_in_thread(slack_tool_config(host, {"platforms": {"slack": {"reply_in_thread": "all"}}})) == "all"
    host["channels"]["slack"]["reply_in_thread"] = False
    assert resolve_reply_in_thread(slack_tool_config(host, {"platforms": {"slack": {}}})) == "none"


def test_normalizes_belle_workspaces_with_host_fallback() -> None:
    host = {"channels": {"slack": {"default_channel_id": "C1"}}}
    belle = {"platforms": {"slack": {"workspaces": [{"bot_token": "x", "app_token": "y"}]}}}

    assert normalize_slack_conf(slack_tool_config(host, belle))["workspaces"] == [
        {"bot_token": "x", "app_token": "y", "default_channel_id": "C1"}
    ]
