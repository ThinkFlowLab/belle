"""Show "<bot> is typing..." while a turn runs, where its answer goes.

A typing notice lasts ten seconds, or until the bot posts in that channel, so it is sent again before it
ends. No call takes a notice back: a turn that ends without a post leaves it up for the rest of the ten
seconds.
"""

import asyncio
import logging
from collections.abc import Callable

from belle_agent.platforms.discord.client import DiscordClient, DiscordError

logger = logging.getLogger(__name__)

#: How long one notice shows, as Discord documents it.
NOTICE_SECONDS = 10.0
#: The notice is sent again before the last one ends, so that one late call leaves no gap.
REPEAT_SECONDS = 8.0


class TypingIndicator:
    """The typing notices of each running turn of one connector."""

    def __init__(self, client: DiscordClient, *, enabled: bool) -> None:
        self._client = client
        self._enabled = enabled
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def start(self, message_id: str, channel: Callable[[], str]) -> None:
        """Send a notice now, and again before it ends, until ``stop`` for this message.

        ``channel`` names where each notice goes when it is sent: the answer's place can change while the turn
        runs, such as a thread started on the message once the answer begins.
        """
        if not self._enabled or message_id in self._tasks:
            return
        self._tasks[message_id] = asyncio.create_task(self._notify(channel), name="discord-typing")

    def stop(self, message_id: str) -> None:
        """Send no more notices for the turn of this message."""
        task = self._tasks.pop(message_id, None)
        if task is not None:
            task.cancel()

    def close(self) -> None:
        """Send no more notices for any turn."""
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    async def _notify(self, channel: Callable[[], str]) -> None:
        place = ""
        try:
            while True:
                place = channel()
                await self._client.request("POST", f"/channels/{place}/typing")
                await asyncio.sleep(REPEAT_SECONDS)
        except DiscordError as exc:
            logger.warning("discord typing indicator stopped for this turn: channel=%s %s", place, exc)
