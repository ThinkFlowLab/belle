
"""JiuwenSwarm bindings for Belle Slack bookmarks tools."""

from belle_agent.integrations.jiuwenswarm.slack_history import _SharedSlackWorkspacesMixin
from belle_agent.integrations.jiuwenswarm.slack_tool_context import slack_bookmark_request_metadata
from belle_agent.integrations.openjiuwen.tools.bookmarks import (
    SlackBookmarkToolkit as _BelleCardToolkit,
)


class SlackBookmarkToolkit(_SharedSlackWorkspacesMixin, _BelleCardToolkit):
    pass


__all__ = ["SlackBookmarkToolkit", "slack_bookmark_request_metadata"]
