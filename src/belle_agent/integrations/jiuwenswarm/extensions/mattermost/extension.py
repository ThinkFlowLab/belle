"""Add Belle's Mattermost channel to JiuwenSwarm, and its tools to the agent."""

from jiuwenswarm.extensions.sdk.contributions import ContributionRegistry
from jiuwenswarm.extensions.sdk.host_types import ChannelSpec

from belle_agent.integrations.jiuwenswarm.config import check_belle_config
from belle_agent.integrations.jiuwenswarm.logs import share_jiuwenswarm_handlers
from belle_agent.integrations.jiuwenswarm.mattermost_channel import create_mattermost_channels
from belle_agent.integrations.jiuwenswarm.mattermost_tools import mount_mattermost_tools
from belle_agent.platforms.mattermost.capabilities import MATTERMOST_CAPABILITIES, NAME
from belle_agent.scopes import register_channel


async def register_extensions(registry: ContributionRegistry) -> None:
    """Register the channel, and the agent plugin that mounts its tools."""
    check_belle_config()
    share_jiuwenswarm_handlers("belle_agent")
    # The AgentServer settles scoped tool permissions for this channel's requests too.
    register_channel(MATTERMOST_CAPABILITIES)
    registry.register_channel_spec(
        ChannelSpec(channel=NAME, config_fields=(), factory=create_mattermost_channels, source="belle-agent")
    )
    registry.register_agent_plugin(mount_mattermost_tools)
