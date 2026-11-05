"""OpenJiuwen binding for Slack's scheduled-message queue."""

from belle_agent.platforms.slack.scheduled import SlackScheduledToolkit as _BelleToolkit

from ._binding import OpenJiuwenToolMixin


class SlackScheduledToolkit(OpenJiuwenToolMixin, _BelleToolkit):
    pass
