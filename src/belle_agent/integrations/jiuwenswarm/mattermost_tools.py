"""Belle's Mattermost tools on JiuwenSwarm's agent, for the turns of the Mattermost channel.

The AgentServer runs the tools, so it holds its own REST client of each
server from Belle's ``platforms.mattermost``. The tools read the turn's
Mattermost metadata, which the channel stamps on each request, and are
mounted for the requests of that channel only.
"""

import logging
from collections.abc import Mapping
from typing import Any

from belle_agent.config import BelleConfigError, load_config, platform_section
from belle_agent.integrations.jiuwenswarm.mattermost_channel import mattermost_config
from belle_agent.integrations.jiuwenswarm.slack_write_confirmation_rail import SlackWriteConfirmationRail
from belle_agent.integrations.openjiuwen.agent_plugin.load import load_plugin
from belle_agent.integrations.openjiuwen.tools._binding import bind_tool_definitions
from belle_agent.paths import selected_config_file
from belle_agent.platforms.base import enabled_platforms
from belle_agent.platforms.mattermost.capabilities import NAME
from belle_agent.platforms.mattermost.client import MattermostClient, MattermostError
from belle_agent.platforms.mattermost.connector import MattermostPlatform
from belle_agent.platforms.mattermost.servers import MattermostServers
from belle_agent.platforms.mattermost.settings import KEY_SERVERS, read_servers, read_settings
from belle_agent.platforms.mattermost.toolkit import WIDENING_TOOLS

logger = logging.getLogger(__name__)


class MattermostRuntimeTools:
    """Mount the Mattermost tools, and their widening confirmation, for Mattermost requests."""

    def __init__(self, agent: Any, request_context: Any, config: Mapping[str, Any]) -> None:
        self._agent = agent
        self._context = request_context
        section = platform_section(config, NAME)
        servers = isinstance(section, Mapping) and bool(section.get(KEY_SERVERS))
        settings = read_servers(config) if servers else [read_settings(config)]
        self._clients = [MattermostClient(entry.url, entry.token) for entry in settings]
        self._members = [MattermostPlatform(entry, client=client) for entry, client in zip(settings, self._clients, strict=True)]
        # The channel's own host starts the servers and stamps each turn with its server. Here they only route.
        platform: Any = MattermostServers(self._members, started_elsewhere=True) if servers else self._members[0]
        (self._toolkit,) = platform.toolkits(self._metadata)
        self._tools = bind_tool_definitions(self._toolkit)
        self._rail: SlackWriteConfirmationRail | None = None
        self._records: dict[str, Any] = {}

    def _metadata(self) -> Mapping[str, Any]:
        """The turn's Mattermost metadata, or nothing for a request of another channel."""
        if self._context.channel_id != NAME or not isinstance(self._context.metadata, Mapping):
            return {}
        return self._context.metadata

    async def _identify(self) -> None:
        """Learn each bot's user id once: a reaction and a direct channel name the bot."""
        for member, client in zip(self._members, self._clients, strict=True):
            if member.bot_user_id:
                continue
            try:
                member.bot_user_id = str((await client.me()).get("id") or "")
            except MattermostError as exc:
                logger.warning("%s did not name the bot, so a reaction or a direct message fails: %s", member.label, exc)

    async def refresh(self) -> None:
        """Mount the tools for a Mattermost request, before its turn runs."""
        if not self._metadata():
            return
        await self._identify()
        if self._rail is None:
            rail = SlackWriteConfirmationRail(self._toolkit, tools=self._tools, tool_names=WIDENING_TOOLS)
            await load_plugin(self._agent, rails=(rail,), skills=False)
            self._rail = rail
        await self.remount(self._tools)

    def rails(self) -> tuple[Any, ...]:
        """The widening confirmation, once mounted."""
        return (self._rail,) if self._rail is not None else ()

    async def remount(self, tools: list[Any]) -> None:
        """Mount ``tools`` again. A name that another tool holds stays that tool's."""
        for tool in tools:
            name = tool.card.name
            if self._agent.ability_manager.get(name) is not None:
                continue  # A tool of that name is mounted already: the operator's, or another platform's.
            self._records[name] = (await load_plugin(self._agent, tools=(tool,), skills=False), tool)

    async def unmount(self) -> list[Any]:
        """Unmount the tools this manager mounted, and return them."""
        unmounted = []
        for name, (record, tool) in list(self._records.items()):
            if self._agent.ability_manager.get(name) is not tool.card:
                continue
            await self._agent.unload_extension(record)
            del self._records[name]
            unmounted.append(tool)
        return unmounted


async def mount_mattermost_tools(services: Any) -> None:
    """Register the Mattermost tools with the agent, where Belle's ``platforms.mattermost`` is on.

    A section that is off, or on in part, mounts nothing, and the reason is logged: the agent's other
    plugins still load.
    """
    try:
        config = mattermost_config(load_config(selected_config_file()))
        enabled_platforms(config)
    except BelleConfigError as exc:
        logger.info("Belle's Mattermost tools are not mounted: %s", exc)
        return
    services.register_runtime_tools(MattermostRuntimeTools(services.agent, services.runtime_context, config))
