
"""JiuwenSwarm bindings for Belle Slack canvases tools."""

from belle_agent.integrations.jiuwenswarm.slack_history import _SharedSlackWorkspacesMixin
from belle_agent.integrations.jiuwenswarm.slack_tool_context import slack_canvas_request_metadata
from belle_agent.integrations.openjiuwen.tools.canvases import (
    SlackCanvasToolkit as _BelleCardToolkit,
)
from belle_agent.platforms.slack.canvases import (
    CANVAS_TOOL_NAMES,
    MAX_CONTENT_CHARS,
    MAX_MARKDOWN_CHARS,
    SECTION_TYPES,
)
from belle_agent.platforms.slack.json_toolkit import (
    ACCESS_LEVELS,
)


class SlackCanvasToolkit(_SharedSlackWorkspacesMixin, _BelleCardToolkit):
    pass


__all__ = [
    "ACCESS_LEVELS",
    "CANVAS_TOOL_NAMES",
    "MAX_CONTENT_CHARS",
    "MAX_MARKDOWN_CHARS",
    "SECTION_TYPES",
    "SlackCanvasToolkit",
    "slack_canvas_request_metadata",
]
