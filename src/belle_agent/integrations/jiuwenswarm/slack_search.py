# ruff: noqa: F401 - moved compatibility exports
"""JiuwenSwarm bindings for Belle Slack search tools."""

from collections.abc import Mapping
from typing import Any

from belle_agent.config import platform_section
from belle_agent.integrations.jiuwenswarm.config import load_belle_config
from belle_agent.integrations.jiuwenswarm.slack_history import _SharedSlackWorkspacesMixin
from belle_agent.integrations.openjiuwen.tools.search import SlackSearchToolkit as _BelleCardToolkit
from belle_agent.platforms.slack.search import (
    _CHANNEL_TYPES,
    _CONFIG_ENABLED_KEY,
    SLACK_ACTION_TOKEN_KEY,
)
from belle_agent.platforms.slack.tool_common import redact_credentials


def slack_search_enabled(config: Mapping[str, Any] | None = None) -> bool:
    """Read the operator's search setting for this request."""
    if config is None:
        config = load_belle_config()
    return bool((platform_section(config, "slack") or {}).get(_CONFIG_ENABLED_KEY, True))


def slack_search_request_metadata(
    channel_id: str | None,
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Require a Slack turn with an action token and search enabled."""
    if str(channel_id or "").strip().lower() != "slack":
        return {}
    if not isinstance(metadata, Mapping):
        return {}
    if not str(metadata.get(SLACK_ACTION_TOKEN_KEY) or "").strip():
        return {}
    if not slack_search_enabled():
        return {}
    return dict(metadata)


class SlackSearchToolkit(_SharedSlackWorkspacesMixin, _BelleCardToolkit):
    pass


__all__ = [
    "SLACK_ACTION_TOKEN_KEY",
    "SlackSearchToolkit",
    "slack_search_enabled",
    "slack_search_request_metadata",
]
