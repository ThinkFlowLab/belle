"""Whether the actor of a Slack event is an app's bot.

A message from an app carries ``bot_id``. A reaction, a pin or a change of members names only the user id of
who did it, so ``users.info`` says whether that user is a bot. Slack's own users are the exception: they post
with no ``bot_id``, and ``users.info`` calls them no bot.
"""

import logging
from collections.abc import Collection, Mapping
from typing import Any

from belle_agent.platforms.slack.delivery import slack_error_code

#: The grant ``users.info`` needs.
USERS_READ = "users:read"
#: The users whose kind is kept. A user's kind does not change, so each is asked about once.
MAX_KNOWN_ACTORS = 4096
#: The user ids of Slack itself in every workspace: Slack, which sends notices such as "You have been removed from
#: #general", and Slackbot. What they post is Slack's own, not a person's.
SLACK_USERS = frozenset({"USLACK", "USLACKBOT"})


class ActorUnknown(Exception):
    """Slack did not say whether the actor is a bot. ``code`` is why, as an outcome names it."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

    @property
    def outcome(self) -> str:
        return f"dropped:actor-unknown:{self.code}"


class SlackActors:
    """The kinds of the users who did something in one workspace, as ``users.info`` gives them."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self._bots: dict[str, bool] = {}
        self._warned_scope = False

    async def is_bot(self, client: Any, actor: str, granted: Collection[str] | None) -> bool:
        """Whether ``actor`` is a bot. Raise ``ActorUnknown`` where Slack does not say."""
        if actor in SLACK_USERS:
            return True
        known = self._bots.get(actor)
        if known is not None:
            return known
        if granted is not None and USERS_READ not in granted:
            if not self._warned_scope:
                self._warned_scope = True
                self._logger.warning(
                    "slack events outside direct messages are dropped: the app lacks %s, so users.info cannot say"
                    " whether a person or an app did them. Reinstall it with this scope", USERS_READ,
                )
            raise ActorUnknown("missing_scope")
        try:
            answer = await client.users_info(user=actor)
        except Exception as exc:  # noqa: BLE001 - SDK error types vary
            code = slack_error_code(exc) or type(exc).__name__
            self._logger.warning(
                "slack event by %s is dropped: users.info answered %s, so it is not known whether a person or an app"
                " did it", actor, code,
            )
            raise ActorUnknown(code) from None
        user = answer.get("user")
        if not isinstance(user, Mapping):
            raise ActorUnknown("no_user")
        while len(self._bots) >= MAX_KNOWN_ACTORS:
            del self._bots[next(iter(self._bots))]
        known = self._bots[actor] = bool(user.get("is_bot"))
        return known
