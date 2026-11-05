"""OpenJiuwen bindings for Slack search tools."""

from belle_agent.platforms.slack.search import SlackSearchToolkit as _BelleToolkit

from ._binding import OpenJiuwenToolMixin


class SlackSearchToolkit(OpenJiuwenToolMixin, _BelleToolkit):
    pass
