
"""JiuwenSwarm bindings for Belle Slack post tools."""

from typing import Any

from belle_agent.integrations.jiuwenswarm.slack_history import (
    shared_slack_workspaces,
)
from belle_agent.integrations.jiuwenswarm.slack_tool_context import SlackCronPostMixin, slack_post_request_metadata
from belle_agent.integrations.openjiuwen.tools.post import SlackPostToolkit as _BelleCardToolkit
from belle_agent.platforms.slack.post import (
    DELETE_MESSAGE,
    EDIT_MESSAGE,
    POST_MESSAGE,
    WIDENING_TOOL_NAMES,
    WRITE_TOOL_NAMES,
)
from belle_agent.platforms.slack.scope_policy import resolve_bot_name


class SlackPostToolkit(SlackCronPostMixin, _BelleCardToolkit):
    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("workspaces", shared_slack_workspaces())
        kwargs.setdefault("bot_name_resolver", resolve_bot_name)
        super().__init__(**kwargs)


__all__ = [
    "DELETE_MESSAGE",
    "EDIT_MESSAGE",
    "POST_MESSAGE",
    "WIDENING_TOOL_NAMES",
    "WRITE_TOOL_NAMES",
    "SlackPostToolkit",
    "slack_post_request_metadata",
]
