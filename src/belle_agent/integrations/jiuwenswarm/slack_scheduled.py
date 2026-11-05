"""JiuwenSwarm binding for Slack's scheduled-message queue."""

from belle_agent.integrations.jiuwenswarm.slack_history import _SharedSlackWorkspacesMixin
from belle_agent.integrations.openjiuwen.tools.scheduled import SlackScheduledToolkit as _BelleCardToolkit


class SlackScheduledToolkit(_SharedSlackWorkspacesMixin, _BelleCardToolkit):
    pass
