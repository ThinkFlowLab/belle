"""Join JiuwenSwarm's Slack connection with Belle's settings."""

from collections.abc import Mapping
from typing import Any

from belle_agent.config import BelleConfigError, platform_section

_HOST_KEYS = frozenset({
    "enabled", "default_channel_id", "allow_from",
    "allowed_channel_ids", "session", "send_file_allowed",
})

_BELLE_KEYS = frozenset({
    "workspaces", "command_name", "app_messages", "app_messages_from",
    "optional_reply_mode", "group_chat_mode", "acknowledge_mode",
    "acknowledgement_text", "thinking_status",
    "queued_status", "steered_status", "session_surface",
    "decision_questions_file", "decision_forward", "sdk_log_level",
    "acknowledgement_emoji", "rejected_emoji", "queued_emoji",
    "completed_emoji", "failed_emoji", "stopped_emoji",
    "brain_react_pending", "brain_react_success", "brain_react_declined",
    "brain_react_failure", "enable_streaming", "activity_card",
    "activity_card_delay_seconds", "activity_card_min_edit_seconds",
    "blockkit_tables", "render_tables", "group_digital_avatar",
    "my_user_id", "principal_name", "bot_name", "enable_memory",
    "home_tab_welcome",
    "canvas_read_max_chars", "directory_scan_max_pages",
})


#: Belle's switch of its Slack section. It is not the host's switch of the same name: JiuwenSwarm's
#: ``channels.slack.enabled`` turns the host's Slack channel on and off.
_BELLE_SWITCH = "enabled"


def slack_section(config: Mapping[str, Any]) -> dict[str, Any]:
    section = platform_section(config, "slack")
    if not isinstance(section, dict):
        raise BelleConfigError("Belle config platforms.slack must be a mapping")
    return section


def slack_tool_config(host_config: dict[str, Any], belle_config: dict[str, Any]) -> dict[str, Any]:
    host = host_config.get("channels", {}).get("slack", {})
    belle = dict(slack_section(belle_config))
    if belle.pop(_BELLE_SWITCH, None) is False:
        raise ValueError(
            "Belle platforms.slack.enabled is false, and the JiuwenSwarm channel serves Slack only. Remove the key or set"
            " it to true. JiuwenSwarm's channels.slack.enabled turns its Slack channel off"
        )
    if overlap := host.keys() & _BELLE_KEYS:
        raise ValueError(f"Move channels.slack settings to Belle platforms.slack: {', '.join(sorted(overlap))}")
    if overlap := belle.keys() & _HOST_KEYS:
        raise ValueError(f"Belle platforms.slack must not set host connector keys: {', '.join(sorted(overlap))}")
    merged = {**host, **belle}
    for key in ("bot_token", "app_token"):
        if value := belle.get(key) or host.get(key):
            merged[key] = value
        else:
            merged.pop(key, None)
    return merged
