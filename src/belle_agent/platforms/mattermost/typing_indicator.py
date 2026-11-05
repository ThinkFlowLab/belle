"""Show "<bot> is typing..." while a turn runs, where its answer goes, as Mattermost's own clients do.

The server relays each notice to the readers of the channel. A web client shows it for the server's
``TimeBetweenUserTypingUpdatesMilliseconds`` after the last notice, or until the bot posts in that
place. No call takes a notice back, so a turn that ends without a post leaves it up to that time.
The server relays a notice whatever ``EnableUserTypingMessages`` says: its own clients read that
setting before they send one, and so does Belle.
"""

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from belle_agent.platforms.base import InboundMessage
from belle_agent.platforms.mattermost.client import MattermostClient, MattermostError
from belle_agent.platforms.mattermost.directory import Directory

logger = logging.getLogger(__name__)

_ENABLED = "EnableUserTypingMessages"
_INTERVAL = "TimeBetweenUserTypingUpdatesMilliseconds"


class TypingIndicator:
    """The typing notices of each running turn of one connector."""

    def __init__(self, client: MattermostClient, directory: Directory, *, enabled: bool) -> None:
        self._client = client
        self._directory = directory
        self._enabled = enabled
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        #: Why the server's settings allow no notice, once it has been logged.
        self._said = ""

    async def start(self, inbound: InboundMessage) -> None:
        """Send a notice now, and again before a client stops showing it, until ``stop``."""
        key = (inbound.conversation, inbound.message_id)
        if not self._enabled or key in self._tasks:
            return
        config = await self._config()
        seconds = None if config is None else self._repeat_seconds(config)
        if seconds is None:
            return
        place = {"channel_id": inbound.conversation, "parent_id": inbound.reply_thread or ""}
        self._tasks[key] = asyncio.create_task(self._notify(place, seconds), name="mattermost-typing")

    def stop(self, inbound: InboundMessage) -> None:
        """Send no more notices for the turn of this message."""
        task = self._tasks.pop((inbound.conversation, inbound.message_id), None)
        if task is not None:
            task.cancel()

    def close(self) -> None:
        """Send no more notices for any turn."""
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    async def _config(self) -> Mapping[str, Any] | None:
        try:
            return await self._directory.client_config()
        except MattermostError as exc:
            logger.warning("mattermost typing indicator not shown: the client settings were not read: %s", exc)
            return None

    def _repeat_seconds(self, config: Mapping[str, Any]) -> float | None:
        """Half the time a client shows one notice, so that one late notice leaves no gap. ``None``: send none."""
        if str(config.get(_ENABLED)).lower() == "false":
            self._say(f"the server sets ServiceSettings.{_ENABLED} to false")
            return None
        try:
            milliseconds = int(str(config.get(_INTERVAL)))
        except ValueError:
            milliseconds = 0
        if milliseconds <= 0:
            self._say(f"the server states no positive ServiceSettings.{_INTERVAL}")
            return None
        return milliseconds / 2000

    def _say(self, reason: str) -> None:
        if reason != self._said:
            self._said = reason
            logger.info("mattermost typing indicator off: %s", reason)

    async def _notify(self, place: Mapping[str, str], seconds: float) -> None:
        try:
            while True:
                await self._client.request("POST", "/users/me/typing", json=place)
                await asyncio.sleep(seconds)
        except MattermostError as exc:
            logger.warning("mattermost typing indicator stopped for this turn: channel=%s %s", place["channel_id"], exc)
