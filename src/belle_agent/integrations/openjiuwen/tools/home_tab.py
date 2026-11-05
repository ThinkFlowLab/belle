"""OpenJiuwen bindings for Slack home_tab tools."""

from belle_agent.platforms.slack.home_tab import SlackHomeTabToolkit as _BelleToolkit

from ._binding import OpenJiuwenToolMixin


class SlackHomeTabToolkit(OpenJiuwenToolMixin, _BelleToolkit):
    pass
