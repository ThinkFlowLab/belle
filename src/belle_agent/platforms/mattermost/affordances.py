"""Mattermost capability declarations."""

from belle_agent.platforms.base import PlatformAffordances

#: The server can refuse a post to one person (``EphemeralRefused``), and a question needs ``actions.url``.
#: Silent and stopped turns remove their cards. Later reply parts go in the first part's thread.
MATTERMOST_AFFORDANCES = PlatformAffordances(
    private_aside="conditional", questions="conditional", silent_card="delete", reply_parts_in_thread=True,
)
