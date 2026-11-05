
"""JiuwenSwarm bindings for Belle Slack pins tools."""

from belle_agent.integrations.jiuwenswarm.slack_history import _SharedSlackWorkspacesMixin
from belle_agent.integrations.jiuwenswarm.slack_tool_context import slack_pin_request_metadata
from belle_agent.integrations.openjiuwen.tools.pins import SlackPinToolkit as _BelleCardToolkit


class SlackPinToolkit(_SharedSlackWorkspacesMixin, _BelleCardToolkit):
    pass


__all__ = ["SlackPinToolkit", "slack_pin_request_metadata"]
