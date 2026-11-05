"""The Home tab welcome of the Slack connector on the platform port.

With ``home_tab_welcome`` on, a person who opens the app's Home tab while it
is empty finds a welcome there. The page is theirs alone. The decision of
which visit is welcomed and the text are those of ``home_tab``, which the
JiuwenSwarm channel shares.

The welcome is no event rule. The connector makes or refuses it before the
visit is handed to the host as an ``app_home`` event, and whatever
``delivery.events`` says of that event. App Home is the person's direct
message with the app, so the list of served conversations does not hold it.
``allow_from`` does.
"""

from typing import Any

from belle_agent.platforms.slack.admission import event_gate
from belle_agent.platforms.slack.history_policy import METADATA_ASKER_KEY, METADATA_TEAM_KEY
from belle_agent.platforms.slack.home_tab import (
    SlackHomeTabToolkit,
    home_tab_welcome_outcome,
    publish_home_tab_welcome,
)


class SlackConnectorHome:
    """Welcome a person onto their empty Home tab."""

    async def _route_app_home_opened(self, event: dict[str, Any], body: dict[str, Any]) -> str:
        if not self.config.home_tab_welcome:
            return "ignored:home-tab-welcome-is-off"
        user_id, _chat_id, outcome = event_gate(event, self._identity.bot_user_id)
        if outcome:
            return outcome
        if not self._host.admits(user_id):
            return "refused:user-not-in-allow_from"
        outcome = home_tab_welcome_outcome(event)
        if outcome is not None:
            return outcome or "ignored:not-the-home-tab"
        # The Home tab tool, bound to the person who opened the tab: a page is published for one person.
        toolkit = SlackHomeTabToolkit(
            metadata={METADATA_ASKER_KEY: user_id, METADATA_TEAM_KEY: self.team_id},
            workspaces=self._workspaces,
        )
        if not await publish_home_tab_welcome(toolkit, bot_name=self.config.bot_name, command=self._slash_command()):
            return "failed:home-tab-welcome-not-published"
        return "welcomed:empty-home-tab"
