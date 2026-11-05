"""Activate Belle's JiuwenSwarm runtime integrations after plugin load."""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from openjiuwen.harness.rails import SkillUseRail

from belle_agent.integrations.jiuwenswarm.job_delivery_rail import JobDeliveryRail
from belle_agent.integrations.jiuwenswarm.skill_prompt import bind_skill_prompt_provider
from belle_agent.integrations.jiuwenswarm.slack_runtime_tools import SlackRuntimeTools
from belle_agent.integrations.openjiuwen.rails.scoped_skill_use_rail import ScopedSkillUseRail, installed_skill_names
from belle_agent.integrations.openjiuwen.rails.silence_rail import SilenceRail
from belle_agent.scopes.skills import bind_scoped_skills, reset_scoped_skills

logger = logging.getLogger(__name__)
_request_metadata: ContextVar[dict[str, Any] | None] = ContextVar("belle_request_metadata", default=None)


def _second_brain_rail():
    try:
        from jiuwenswarm.extensions.sdk.workspace_paths import get_agent_workspace_dir

        from belle_agent.integrations.jiuwenswarm.config import load_belle_config
        from belle_agent.integrations.jiuwenswarm.second_brain_backend import SECOND_BRAIN_BACKEND
        from belle_agent.integrations.openjiuwen.rails.second_brain_rail import SecondBrainRail
        from belle_agent.second_brain.paths import resolve_second_brain_dir, second_brain_config

        config = load_belle_config()
        section = second_brain_config(config)
        if not section["enabled"]:
            return None
        retrieval = section.get("retrieval")
        retrieval = retrieval if isinstance(retrieval, dict) else {}
        return SecondBrainRail(
            corpus_dir=resolve_second_brain_dir(config, workspace_dir=get_agent_workspace_dir()),
            top_k=int(retrieval.get("top_k") or 5),
            min_score=float(retrieval.get("min_score") or 0.35),
            max_total_bytes=int(retrieval.get("max_total_bytes") or 10240),
            config=config,
            backend=SECOND_BRAIN_BACKEND,
        )
    except Exception:
        logger.exception("SecondBrainRail creation failed")
        return None


def rails(agent: Any) -> tuple[Any, ...]:
    skills = agent.find_rails_by_type((SkillUseRail,))
    return tuple(rail for rail in (
        ScopedSkillUseRail(skills[0]) if skills else None,
        _second_brain_rail(),
        SilenceRail(_request_metadata.get),
    ) if rail is not None)


@contextmanager
def _request_scope(agent: Any, request: Any):
    params = request.params if isinstance(request.params, dict) else {}
    bind_skill_prompt_provider(agent)
    if params.get("agent_skills_required") and not agent.find_rails_by_type((ScopedSkillUseRail,)):
        raise ValueError("Required Skills need an active Skill rail")
    skill_rails = agent.find_rails_by_type((SkillUseRail,))
    tokens = bind_scoped_skills(params, installed_skill_names(skill_rails[0]) if skill_rails else None)
    metadata = request.metadata if isinstance(request.metadata, dict) else None
    metadata_token = _request_metadata.set(metadata)
    try:
        yield
    finally:
        _request_metadata.reset(metadata_token)
        reset_scoped_skills(tokens)


def activate(
    agent: Any,
    register_request_context: Any,
    register_runtime_tools: Any,
    runtime_context: Any,
) -> None:
    from belle_agent.integrations.jiuwenswarm import scope_permissions

    bind_skill_prompt_provider(agent)
    scope_permissions.activate()
    register_request_context(lambda request: _request_scope(agent, request))
    register_runtime_tools(SlackRuntimeTools(agent, runtime_context))


async def mount_plugin(services: Any) -> Any | None:
    from belle_agent.integrations.jiuwenswarm.content_tools import configured_content_tools
    from belle_agent.integrations.openjiuwen.agent_plugin.load import plugin_spec

    context = services.runtime_context
    selected_rails = (*rails(services.agent), JobDeliveryRail(lambda: getattr(context, "channel_id", "")))
    record = await services.load_plugin_spec(plugin_spec(rails=selected_rails, tools=configured_content_tools(services.runtime_context)))
    if record is not None:
        activate(
            services.agent,
            services.register_request_context,
            services.register_runtime_tools,
            services.runtime_context,
        )
    return record
