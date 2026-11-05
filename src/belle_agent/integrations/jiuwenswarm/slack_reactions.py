
"""JiuwenSwarm bindings for Belle Slack reactions tools."""

from belle_agent.integrations.jiuwenswarm.slack_history import _SharedSlackWorkspacesMixin
from belle_agent.integrations.openjiuwen.tools.reactions import (
    SlackReactionToolkit as _BelleCardToolkit,
)
from belle_agent.platforms.slack.reactions import (
    SLACK_CHANNEL_ID_KEY,
    SLACK_MESSAGE_TS_KEY,
    slack_reaction_request_metadata,
)


class SlackReactionToolkit(_SharedSlackWorkspacesMixin, _BelleCardToolkit):
    pass


__all__ = [
    "SLACK_CHANNEL_ID_KEY",
    "SLACK_MESSAGE_TS_KEY",
    "SlackReactionToolkit",
    "slack_reaction_request_metadata",
]
