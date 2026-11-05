"""Shared Gateway lifecycle for Belle conversation channels."""

import asyncio
import inspect
import logging
from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import Any

from jiuwenswarm.extensions.sdk.channel_runtime import BaseChannel, ChannelMetadata, RoutingTarget
from jiuwenswarm.extensions.sdk.host_types import Message

from belle_agent.backend import BackendError
from belle_agent.config import BelleConfigError
from belle_agent.host import ConversationHost
from belle_agent.integrations.jiuwenswarm.channel_turns import JiuwenSwarmTurns

logger = logging.getLogger(__name__)


def channel_config(config: Mapping[str, Any], name: str) -> dict[str, Any]:
    """Keep one platform. The AgentServer owns Second Brain."""
    platforms = config.get("platforms") or {}
    return {
        **config,
        "platforms": {key: value for key, value in platforms.items() if key == name},
        "second_brain": {"enabled": False},
    }


class ConversationChannel(BaseChannel):
    """Route Belle turns through the Gateway and return events to their turns."""

    host: ConversationHost
    turns: JiuwenSwarmTurns
    renders_interactive_prompts = True

    def __init__(self, router: Any) -> None:
        # Belle's host enforces admission from its platform settings.
        super().__init__(SimpleNamespace(allow_from=[]), router)
        self._on_message: Callable[[Message], Any] | None = None
        self._stopped = asyncio.Event()

    @property
    def channel_id(self) -> str:
        return self.name

    def on_message(self, callback: Callable[[Message], Any]) -> None:
        self._on_message = callback

    async def _route(self, msg: Message) -> None:
        if self._on_message is None:
            await self.bus.route_user_message(msg)
            return
        result = self._on_message(msg)
        if inspect.isawaitable(result):
            await result

    async def start(self) -> None:
        self._stopped.clear()
        self._running = True
        try:
            await self._start_host()
            await self._stopped.wait()
        finally:
            self._running = False

    async def _start_host(self) -> None:
        """Start the host, and log why it did not start: the Gateway keeps the start task's exception unread."""
        try:
            await self.host.start()
        except (BelleConfigError, BackendError, OSError) as exc:
            # As the launcher does: the message names what to fix.
            logger.critical("%s cannot start: %s", self.name, exc)
            raise
        except Exception:
            logger.exception("%s cannot start", self.name)
            raise

    async def stop(self) -> None:
        self._running = False
        try:
            await self.host.stop()
        finally:
            self._stopped.set()

    async def send(self, msg: Message, *, routing_target: RoutingTarget | None = None) -> None:
        if not self.turns.deliver(msg):
            logger.debug("%s dropped an event of no running turn: id=%s type=%s", self.name, msg.id, msg.event_type)

    def get_metadata(self) -> ChannelMetadata:
        return ChannelMetadata(channel_id=self.name, source=self.name)
