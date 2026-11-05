"""OpenJiuwen bindings for Slack pins tools."""

from belle_agent.platforms.slack.pins import SlackPinToolkit as _BelleToolkit

from ._binding import OpenJiuwenToolMixin


class SlackPinToolkit(OpenJiuwenToolMixin, _BelleToolkit):
    pass
