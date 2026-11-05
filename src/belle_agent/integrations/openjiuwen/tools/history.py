"""OpenJiuwen bindings for Slack history tools."""

from belle_agent.platforms.slack.history import SlackHistoryToolkit as _BelleToolkit

from ._binding import OpenJiuwenToolMixin


class SlackHistoryToolkit(OpenJiuwenToolMixin, _BelleToolkit):
    pass
