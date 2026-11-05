"""Apply one-turn OpenJiuwen skill and subagent requirements."""

from typing import Any

from belle_agent.capability_catalog import scope_allows


def apply_turn_requirements(
    params: dict[str, Any], *, required_skills: tuple[str, ...],
    required_subagent: tuple[str, str] | None, clear_extensions: bool,
) -> str | None:
    """Apply one-turn requirements within the scoped skill and agent catalog."""
    if clear_extensions:
        params["plugin_names"] = []
        params["mcp"] = []
    if required_skills:
        available = params.get("agent_skills_available")
        unavailable = params.get("agent_skills_unavailable", ())
        if not all(scope_allows(name, available, unavailable) for name in required_skills):
            return "refused:required-skill-outside-the-scoped-catalog"
        params["agent_skills_required"] = list(dict.fromkeys(
            [*params.get("agent_skills_required", ()), *required_skills]
        ))
    if required_subagent is not None:
        name, task = required_subagent
        available = params.get("agent_subagents_available")
        if available is not None and name not in available:
            return "refused:required-subagent-outside-the-scoped-catalog"
        params["agent_subagent_required"] = {"name": name, "task": task}
    return None
