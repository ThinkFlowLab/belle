"""OpenJiuwen bindings for Slack lists tools."""

from belle_agent.platforms.slack.lists import SlackListToolkit as _BelleToolkit

from ._binding import OpenJiuwenToolMixin


class SlackListToolkit(OpenJiuwenToolMixin, _BelleToolkit):
    pass
