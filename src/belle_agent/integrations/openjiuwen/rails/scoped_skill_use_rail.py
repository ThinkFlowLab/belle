"""Carry the per-request Skill scope and load the required Skills."""

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.harness.rails import SkillUseRail
from openjiuwen.harness.rails.base import DeepAgentRail

from belle_agent.integrations.openjiuwen.rails.run_context import with_run_context_extra
from belle_agent.scopes.skills import _REQUIRED, _REQUIRED_CONTENT, bind_scoped_skills, reset_scoped_skills

SCOPED_SKILLS_RUN_CONTEXT_KEY = "belle.scoped_skills"
_ROUND_TOKENS_KEY = "_scoped_skill_tokens"
_MODEL_TOKENS_KEY = "_scoped_skill_model_tokens"
_TOOL_TOKENS_KEY = "_scoped_skill_tool_tokens"


def with_scoped_skill_run_context(
    inputs: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    """Pass Skill settings to the next SDK round."""
    skill_params = {
        key: params[key]
        for key in ("agent_skills_available", "agent_skills_unavailable", "agent_skills_required")
        if key in params
    }
    if not skill_params:
        return inputs
    return with_run_context_extra(inputs, SCOPED_SKILLS_RUN_CONTEXT_KEY, skill_params)


def scoped_skill_params(run_context: Any) -> dict[str, Any] | None:
    """The Skill settings a run context carries, or ``None`` if it carries none."""
    extra = getattr(run_context, "extra", None)
    params = (
        extra.get(SCOPED_SKILLS_RUN_CONTEXT_KEY) if isinstance(extra, Mapping) else None
    )
    if params is None:
        return None
    if not isinstance(params, dict):
        raise TypeError("Invalid scoped Skill run context")
    return params


def installed_skill_names(skill_rail: SkillUseRail) -> Callable[[], list[str]]:
    """The names of the Skills ``skill_rail`` holds, read when a scope needs them."""
    return lambda: [skill.name for skill in skill_rail.skills]


def _bind_inner_scope(ctx: Any, key: str, installed: Callable[[], list[str]]) -> None:
    """Restore the scope in an SDK worker task."""
    owner = (id(ctx), asyncio.current_task())
    if key in ctx.extra and ctx.extra[key][:2] == owner:
        return
    params = scoped_skill_params(ctx.extra.get("run_context"))
    if params is None:
        return
    ctx.extra[key] = (*owner, bind_scoped_skills(params, installed))


def _reset_inner_scope(ctx: Any, key: str) -> None:
    entry = ctx.extra.pop(key, None)
    if entry is not None and entry[:2] == (id(ctx), asyncio.current_task()):
        reset_scoped_skills(entry[2])


class ScopedSkillUseRail(DeepAgentRail):
    """Append required Skill text after the native ``SkillUseRail`` catalog.

    Register immediately after ``SkillUseRail`` and before prompt retrieval.
    Equal priorities preserve registration order.
    """

    priority = SkillUseRail.priority

    def __init__(self, skill_rail: SkillUseRail) -> None:
        super().__init__()
        self._skill_rail = skill_rail

    async def before_invoke(self, ctx: Any) -> None:
        # SDK hot reload can register the same rail callback twice.
        if _ROUND_TOKENS_KEY in ctx.extra:
            return
        inputs = getattr(ctx, "inputs", None)
        params = scoped_skill_params(getattr(inputs, "run_context", None))
        tokens = bind_scoped_skills(params if params is not None else {}, installed_skill_names(self._skill_rail))
        ctx.extra[_ROUND_TOKENS_KEY] = tokens
        try:
            self._load_required_skills()
        except BaseException:
            ctx.extra.pop(_ROUND_TOKENS_KEY, None)
            reset_scoped_skills(tokens)
            raise

    async def after_invoke(self, ctx: Any) -> None:
        tokens = ctx.extra.pop(_ROUND_TOKENS_KEY, None)
        if tokens is not None:
            reset_scoped_skills(tokens)

    async def before_model_call(self, ctx: Any) -> None:
        _bind_inner_scope(ctx, _MODEL_TOKENS_KEY, installed_skill_names(self._skill_rail))
        try:
            self._load_required_skills()
        except BaseException:
            _reset_inner_scope(ctx, _MODEL_TOKENS_KEY)
            raise

    async def after_model_call(self, ctx: Any) -> None:
        _reset_inner_scope(ctx, _MODEL_TOKENS_KEY)

    on_model_exception = after_model_call

    async def before_tool_call(self, ctx: Any) -> None:
        _bind_inner_scope(ctx, _TOOL_TOKENS_KEY, installed_skill_names(self._skill_rail))

    async def after_tool_call(self, ctx: Any) -> None:
        _reset_inner_scope(ctx, _TOOL_TOKENS_KEY)

    on_tool_exception = after_tool_call

    def _load_required_skills(self) -> None:
        """Merge the required Skill text into the native Skills section."""
        required = _REQUIRED.get()
        if not required:
            _REQUIRED_CONTENT.set("")
            return
        available = {skill.name: skill for skill in self._skill_rail.skills}
        missing = [name for name in required if name not in available]
        if missing:
            raise ValueError(
                "Required Skills are not installed or enabled: "
                + ", ".join(missing)
            )
        parts = [
            (
                "# Required Skills\n"
                "The `SKILL.md` text for each required Skill is below. "
                "Follow each instruction on this turn according to its stated conditions. "
                "You do not need `skill_tool` to read this text."
            )
        ]
        for name in required:
            path = available[name].directory / "SKILL.md"
            try:
                content = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ValueError(f"Required Skill {name!r} cannot be read: {exc}") from exc
            parts.append(f"## Required Skill: {name}\n\n{content}")
        builder = self._skill_rail.system_prompt_builder
        if builder is None:
            raise ValueError("Required Skills need a system prompt builder")
        prompt_mode = getattr(builder, "mode", None)
        if getattr(prompt_mode, "value", prompt_mode) == "none":
            raise ValueError("Required Skills cannot load with prompt_mode=none")
        required_content = "\n\n".join(parts)
        _REQUIRED_CONTENT.set(required_content)
        language = builder.language
        native = builder.get_section(SectionName.SKILLS)
        catalog = native.render(language) if native is not None else ""
        priority = native.priority if native is not None else 50
        if registry := getattr(builder, "priority_registry", None):
            priority = registry.priority_for(SectionName.SKILLS, priority)
        builder.add_section(
            PromptSection(
                name=SectionName.SKILLS,
                content={language: "\n\n".join(part for part in (catalog, required_content) if part)},
                priority=priority,
            )
        )
