"""Register Belle's Matrix channel in JiuwenSwarm."""

from jiuwenswarm.extensions.sdk.contributions import ContributionRegistry
from jiuwenswarm.extensions.sdk.host_types import ChannelSpec

from belle_agent.integrations.jiuwenswarm.config import check_belle_config
from belle_agent.integrations.jiuwenswarm.logs import share_jiuwenswarm_handlers
from belle_agent.integrations.jiuwenswarm.matrix_channel import create_matrix_channels
from belle_agent.platforms.matrix.capabilities import MATRIX_CAPABILITIES, NAME
from belle_agent.scopes import register_channel


async def register_extensions(registry: ContributionRegistry) -> None:
    check_belle_config()
    share_jiuwenswarm_handlers("belle_agent")
    register_channel(MATRIX_CAPABILITIES)
    registry.register_channel_spec(
        ChannelSpec(channel=NAME, config_fields=(), factory=create_matrix_channels, source="belle-agent")
    )
