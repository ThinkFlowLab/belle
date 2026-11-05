from jiuwenswarm.extensions.sdk.contributions import ContributionRegistry

from belle_agent.integrations.jiuwenswarm.config import check_belle_config
from belle_agent.integrations.jiuwenswarm.logs import share_jiuwenswarm_handlers
from belle_agent.integrations.jiuwenswarm.second_brain_adapter import SecondBrainAdapter


async def register_extensions(registry: ContributionRegistry) -> None:
    check_belle_config()
    share_jiuwenswarm_handlers("belle_agent")
    registry.register_gateway_adapter(SecondBrainAdapter())
