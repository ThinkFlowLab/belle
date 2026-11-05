"""OpenJiuwen bindings for Slack directory tools."""

from belle_agent.platforms.slack.directory import SlackDirectoryToolkit as _BelleToolkit

from ._binding import OpenJiuwenToolMixin


class SlackDirectoryToolkit(OpenJiuwenToolMixin, _BelleToolkit):
    pass
