"""One-turn skill and subagent requirements for OpenJiuwen requests."""

from belle_agent.integrations.openjiuwen.turn_requirements import apply_turn_requirements


def test_scoped_one_turn_requirements():
    params = {
        "agent_skills_available": ["review"],
        "agent_skills_required": ["existing"],
        "agent_subagents_available": ["coder"],
    }
    assert apply_turn_requirements(
        params, required_skills=("review", "review"),
        required_subagent=("coder", "task"), clear_extensions=True,
    ) is None
    assert params["agent_skills_required"] == ["existing", "review"]
    assert params["agent_subagent_required"] == {"name": "coder", "task": "task"}
    assert params["plugin_names"] == params["mcp"] == []
    assert apply_turn_requirements(
        {}, required_skills=("missing",), required_subagent=None,
        clear_extensions=False,
    ) is None
    assert apply_turn_requirements(
        {"agent_skills_available": []}, required_skills=("missing",),
        required_subagent=None, clear_extensions=False,
    ) == "refused:required-skill-outside-the-scoped-catalog"


def test_a_required_skill_the_scope_makes_unavailable_is_refused():
    params = {"agent_skills_unavailable": ["review"]}
    assert apply_turn_requirements(
        params, required_skills=("review",), required_subagent=None, clear_extensions=False,
    ) == "refused:required-skill-outside-the-scoped-catalog"
    assert "agent_skills_required" not in params
    assert apply_turn_requirements(
        params, required_skills=("other",), required_subagent=None, clear_extensions=False,
    ) is None
