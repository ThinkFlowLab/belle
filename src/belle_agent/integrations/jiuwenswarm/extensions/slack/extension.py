"""Replace JiuwenSwarm's built-in Slack channel with Belle's connector."""

from jiuwenswarm.extensions.sdk.channel_runtime import make_builtin_delivery_target
from jiuwenswarm.extensions.sdk.contributions import ContributionRegistry
from jiuwenswarm.extensions.sdk.host_types import ChannelSpec

from belle_agent.integrations.jiuwenswarm.config import check_belle_config, slack_default_channel_id_from_config
from belle_agent.integrations.jiuwenswarm.logs import share_jiuwenswarm_handlers
from belle_agent.integrations.jiuwenswarm.slack_channel import warn_if_heartbeat_relay_unreachable
from belle_agent.integrations.jiuwenswarm.slack_gateway import create_slack_channels


async def register_extensions(registry: ContributionRegistry) -> None:
    check_belle_config()
    share_jiuwenswarm_handlers("belle_agent")
    registry.register_channel_spec(
        ChannelSpec(
            channel="slack",
            config_fields=(),
            delivery=make_builtin_delivery_target,
            factory=create_slack_channels,
            check_relay=lambda target, config: warn_if_heartbeat_relay_unreachable(
                heartbeat_target=target,
                default_channel_id=slack_default_channel_id_from_config(config),
            ),
            replaces="slack",
            source="belle-agent",
        )
    )
