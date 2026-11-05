"""OpenJiuwen bindings for Slack bookmarks tools."""

from belle_agent.platforms.slack.bookmarks import SlackBookmarkToolkit as _BelleToolkit

from ._binding import OpenJiuwenToolMixin


class SlackBookmarkToolkit(OpenJiuwenToolMixin, _BelleToolkit):
    pass
