"""OpenJiuwen bindings for Slack reactions tools."""

from belle_agent.platforms.slack.reactions import SlackReactionToolkit as _BelleToolkit

from ._binding import OpenJiuwenToolMixin


class SlackReactionToolkit(OpenJiuwenToolMixin, _BelleToolkit):
    pass
