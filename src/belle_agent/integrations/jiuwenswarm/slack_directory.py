
"""JiuwenSwarm bindings for Belle Slack directory tools."""

from belle_agent.integrations.jiuwenswarm.slack_history import _SharedSlackWorkspacesMixin
from belle_agent.integrations.openjiuwen.tools.directory import (
    SlackDirectoryToolkit as _BelleCardToolkit,
)
from belle_agent.platforms.slack.directory import (
    KIND_CHAT,
    KIND_EMOJI,
    KIND_PERSON,
    KIND_USERGROUP,
    SLACK_KINDS,
    slack_find_request_metadata,
)


class SlackDirectoryToolkit(_SharedSlackWorkspacesMixin, _BelleCardToolkit):
    pass

__all__ = [
    "KIND_CHAT",
    "KIND_EMOJI",
    "KIND_PERSON",
    "KIND_USERGROUP",
    "SLACK_KINDS",
    "SlackDirectoryToolkit",
    "slack_find_request_metadata",
]
