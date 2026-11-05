"""Independent Discord applications behind one installation-routed platform port."""

import logging
from typing import Any

from belle_agent.config import BelleConfigError
from belle_agent.platforms.installations import Installations
from belle_agent.tools.host import refusal

logger = logging.getLogger(__name__)


def bot_of(workspace: str) -> str:
    """Read a bot name only from an installation-qualified workspace."""
    name, separator, place = workspace.partition(":")
    return name if separator and place else ""


def _unknown_tool(name: str, bot: str) -> str | None:
    if name == "widening_question":
        return None
    return refusal("discord_bot_unknown", "this turn names no served Discord bot, so nothing is sent")


class DiscordBots(Installations):
    """Each bot owns its transport, interactions, command registration and caches.

    Aggregate terminal health becomes down only when every started bot is down.
    ``health`` reports each configured bot independently.
    """

    def __init__(self, members: list[Any]) -> None:
        super().__init__(
            {member.bot: member for member in members}, noun="bot", where="platforms.discord.bots",
            installation_of=bot_of, metadata_key="discord_bot", unknown_tool=_unknown_tool, logger=logger,
        )

    @property
    def health(self) -> dict[str, str]:
        """Report each bot as up, terminally down, connecting, or not served."""
        return {
            name: member.stopped
            or ("up" if name in self.serving else "connecting" if name in self.connecting else "not served")
            for name, member in self.members.items()
        }

    async def withdraw_question(self, question_id: str, *, workspace: str = "") -> None:
        """Remove controls only from the bot of the question's held turn."""
        await self._member(workspace).withdraw_question(question_id, workspace=workspace)

    async def refuse_twice_served(self, name: str, member: Any) -> None:
        for other in self.serving.values():
            if member.bot_user_id == other.bot_user_id:
                raise BelleConfigError("Discord bots entries resolve to the same bot account")
