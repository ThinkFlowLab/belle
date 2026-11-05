
"""JiuwenSwarm bindings for Belle Slack home_tab tools."""

from belle_agent.integrations.jiuwenswarm.slack_history import _SharedSlackWorkspacesMixin
from belle_agent.integrations.openjiuwen.tools.home_tab import (
    SlackHomeTabToolkit as _BelleCardToolkit,
)
from belle_agent.platforms.slack.home_tab import (
    HOME_TAB_TOOL_NAMES,
    slack_home_tab_request_metadata,
)


class SlackHomeTabToolkit(_SharedSlackWorkspacesMixin, _BelleCardToolkit):
    pass


__all__ = [
    "HOME_TAB_TOOL_NAMES",
    "SlackHomeTabToolkit",
    "slack_home_tab_request_metadata",
]
