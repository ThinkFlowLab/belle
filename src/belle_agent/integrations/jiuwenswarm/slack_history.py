# ruff: noqa: F401 - history compatibility exports
"""JiuwenSwarm bindings for Belle Slack history tools."""

from collections.abc import Callable
from typing import Any

from belle_agent.integrations.jiuwenswarm.config import load_belle_config
from belle_agent.integrations.jiuwenswarm.slack_tool_config import slack_tool_config
from belle_agent.integrations.jiuwenswarm.slack_tool_context import SlackCronHistoryMixin
from belle_agent.integrations.openjiuwen.tools.history import (
    SlackHistoryToolkit as _BelleCardToolkit,
)
from belle_agent.platforms.slack.history import (
    _ALLOWED_CHANNEL_TYPES,
    _DEFAULT_MAX_FILES_PER_MESSAGE,
    _SLACK_TS_SHAPE,
    _TARGET_ARGUMENT_PROSE,
    _TARGET_RULE_PROSE,
    FILE_TRANSFER_TIMEOUT_SECONDS,
    MAX_FILE_BYTES,
    _iso_utc,
    _retry_after_seconds,
    _slack_message_ts,
    redact_credentials,
)
from belle_agent.platforms.slack.history import (
    SlackWorkspaceClients as _BelleWorkspaceClients,
)
from belle_agent.platforms.slack.history_policy import (
    METADATA_ASKER_KEY,
    METADATA_ENFORCE_AUDIENCE_KEY,
)
from belle_agent.platforms.slack.identity import slack_team_id_from_session
from belle_agent.platforms.slack.rich_text_walk import (
    MAX_RICH_TEXT_DEPTH,
    MAX_RICH_TEXT_STRINGS,
)
from belle_agent.platforms.slack.tool_context import (
    SLACK_PERMALINK_RULE,
    SLACK_TS_RULE,
)


class SlackSessionTeamMixin:
    def _request_team_id(self, metadata=None) -> str:
        return super()._request_team_id(metadata) or slack_team_id_from_session(self._runtime_session_id())


def get_agent_sessions_dir():
    from jiuwenswarm.extensions.sdk.workspace_paths import get_agent_sessions_dir as get_dir

    return get_dir()


def _slack_tool_config() -> dict[str, Any]:
    from jiuwenswarm.extensions.sdk.host_config import get_config

    settings = get_config() or {}
    return slack_tool_config(settings, load_belle_config())


class SlackWorkspaceClients(_BelleWorkspaceClients):
    def __init__(self, client_factory: Callable[[str], Any] | None = None) -> None:
        super().__init__(
            client_factory=client_factory,
            config_loader=_slack_tool_config,
        )


_SHARED_WORKSPACE_CLIENTS = SlackWorkspaceClients()


def shared_slack_workspaces() -> SlackWorkspaceClients:
    return _SHARED_WORKSPACE_CLIENTS


class _SharedSlackWorkspacesMixin:
    def __init__(self, **kwargs: Any) -> None:
        kwargs["workspaces"] = kwargs.get("workspaces") or shared_slack_workspaces()
        super().__init__(**kwargs)


class SlackHistoryToolkit(SlackSessionTeamMixin, SlackCronHistoryMixin, _BelleCardToolkit):
    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("workspaces", shared_slack_workspaces())
        kwargs.setdefault("sessions_dir_provider", lambda: get_agent_sessions_dir())
        super().__init__(**kwargs)


__all__ = ["SlackHistoryToolkit"]
