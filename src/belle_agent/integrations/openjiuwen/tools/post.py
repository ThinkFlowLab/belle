"""OpenJiuwen bindings for Slack post tools."""

from belle_agent.platforms.slack.post import SlackPostToolkit as _BelleToolkit

from ._binding import OpenJiuwenToolMixin


class SlackPostToolkit(OpenJiuwenToolMixin, _BelleToolkit):
    pass
