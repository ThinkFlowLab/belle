"""Mount Belle's OpenJiuwen agent plugin in JiuwenSwarm."""

from typing import Any

from jiuwenswarm.extensions.sdk.contributions import ContributionRegistry

from belle_agent.integrations.jiuwenswarm import slack_routing
from belle_agent.integrations.jiuwenswarm.agent_runtime import mount_plugin
from belle_agent.integrations.jiuwenswarm.config import check_belle_config
from belle_agent.integrations.jiuwenswarm.logs import share_jiuwenswarm_handlers
from belle_agent.integrations.jiuwenswarm.slack_tool_context import slack_place_fields
from belle_agent.integrations.openjiuwen.rails.scoped_skill_use_rail import (
    with_scoped_skill_run_context,
)
from belle_agent.integrations.openjiuwen.scoped_subagents import apply_scoped_subagent_availability
from belle_agent.scopes.skills import scoped_skill_names, unrestricted_skill_inventory
from belle_agent.silence import is_no_reply_sentinel, silence_is_unstored


class _SlackCronHooks:
    mutation = staticmethod(slack_routing.slack_cron_mutation)
    run_metadata = staticmethod(slack_routing.slack_history_metadata_for_cron_job)


def _suppress_silent_final(request: Any, content: str) -> bool:
    return silence_is_unstored(request.metadata) and is_no_reply_sentinel(content)


def _prepare_scoped_inputs(agent: Any, params: dict, inputs: dict) -> dict:
    apply_scoped_subagent_availability(
        agent, params, active_round=getattr(agent, "active_round", None) is not None
    )
    return with_scoped_skill_run_context(inputs, params)


async def register_extensions(registry: ContributionRegistry) -> None:
    check_belle_config()
    share_jiuwenswarm_handlers("belle_agent")
    registry.register_agent_plugin(mount_plugin)
    registry.register_turn_envelope_fields(slack_place_fields)
    registry.register_final_filter(_suppress_silent_final)
    registry.register_agent_input_transform(_prepare_scoped_inputs)
    registry.register_skill_inventory(unrestricted_skill_inventory, scoped_skill_names)
    registry.register_cron_hooks("slack", _SlackCronHooks())
