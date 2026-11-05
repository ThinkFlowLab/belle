"""Connect Belle's per-turn Skill policy to JiuwenSwarm's retrieval prompt."""

from typing import Any

from jiuwenswarm.extensions.sdk.skill_prompt import attach_skill_prompt_provider

from belle_agent.scopes.skills import required_skill_text, scoped_skill_names


def _skill_prompt() -> tuple[frozenset[str] | None, str]:
    return scoped_skill_names(), required_skill_text()


def bind_skill_prompt_provider(agent: Any) -> None:
    attach_skill_prompt_provider(agent, _skill_prompt)
