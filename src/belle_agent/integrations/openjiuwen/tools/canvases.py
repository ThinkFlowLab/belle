"""OpenJiuwen bindings for Slack canvases tools."""

from belle_agent.platforms.slack.canvases import SlackCanvasToolkit as _BelleToolkit

from ._binding import OpenJiuwenToolMixin


class SlackCanvasToolkit(OpenJiuwenToolMixin, _BelleToolkit):
    pass
