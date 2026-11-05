"""Scoped Skill state is isolated to the active turn."""

import asyncio

import pytest

pytest.importorskip("openjiuwen")

from belle_agent.integrations.openjiuwen.rails.scoped_skill_use_rail import (
    SCOPED_SKILLS_RUN_CONTEXT_KEY,
    with_scoped_skill_run_context,
)
from belle_agent.scopes.skills import (
    bind_scoped_skills,
    required_skill_text,
    reset_scoped_skills,
)


def test_run_context_carries_scoped_skills_without_mutating_input() -> None:
    inputs = {"run": {"context": {"extra": {"other": 1}}}}
    result = with_scoped_skill_run_context(inputs, {"agent_skills_required": ["ambient"]})

    assert result["run"]["context"]["extra"] == {
        "other": 1,
        SCOPED_SKILLS_RUN_CONTEXT_KEY: {"agent_skills_required": ["ambient"]},
    }
    assert inputs["run"]["context"]["extra"] == {"other": 1}


def test_required_skill_text_follows_the_native_catalog(tmp_path) -> None:
    from types import SimpleNamespace

    from openjiuwen.harness.prompts import PromptSection
    from openjiuwen.harness.prompts.sections import SectionName
    from openjiuwen.harness.rails import SkillUseRail

    from belle_agent.integrations.openjiuwen.rails.scoped_skill_use_rail import (
        ScopedSkillUseRail,
    )

    skill_dir = tmp_path / "ambient"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: ambient\n---\nAmbient instructions", encoding="utf-8"
    )
    sections = {
        SectionName.SKILLS: PromptSection(
            name=SectionName.SKILLS, content={"en": "native catalog"}, priority=50
        )
    }
    skill_rail = SkillUseRail(str(tmp_path), skill_mode="all", include_tools=False)
    skill_rail.system_prompt_builder = SimpleNamespace(
        language="en",
        get_section=sections.get,
        add_section=lambda section: sections.__setitem__(section.name, section),
    )
    asyncio.run(skill_rail.reload_skills())
    rail = ScopedSkillUseRail(skill_rail)

    tokens = bind_scoped_skills({"agent_skills_required": ["ambient"]})
    try:
        rail._load_required_skills()
        content = sections[SectionName.SKILLS].render("en")
        assert content.startswith("native catalog\n\n# Required Skills\n")
        assert "Ambient instructions" in content
        assert "Ambient instructions" in required_skill_text()
        # The built-in rail rewrites the native section before every hook, so
        # the next merge replaces this one instead of stacking a second copy.
        skill_rail.system_prompt_builder.add_section(
            PromptSection(
                name=SectionName.SKILLS, content={"en": "native catalog"}, priority=50
            )
        )
        rail._load_required_skills()
        assert sections[SectionName.SKILLS].render("en").count("# Required Skills") == 1
    finally:
        reset_scoped_skills(tokens)

    rail._load_required_skills()
    assert required_skill_text() == ""


def test_required_skill_uses_host_priority_when_native_section_is_absent(tmp_path) -> None:
    from openjiuwen.harness.prompts import SystemPromptBuilder
    from openjiuwen.harness.prompts.sections import SectionName
    from openjiuwen.harness.rails import SkillUseRail

    from belle_agent.integrations.openjiuwen.rails.scoped_skill_use_rail import ScopedSkillUseRail

    skill_dir = tmp_path / "ambient"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: ambient\n---\nAmbient instructions")
    skill_rail = SkillUseRail(str(tmp_path), skill_mode="all", include_tools=False)
    asyncio.run(skill_rail.reload_skills())

    class Registry:
        def priority_for(self, name, fallback):
            return 40 if name == SectionName.SKILLS else fallback

        def register_section(self, name, priority):
            assert name != SectionName.SKILLS or priority == 40

    builder = SystemPromptBuilder(language="en").set_priority_registry(Registry())
    skill_rail.system_prompt_builder = builder
    rail = ScopedSkillUseRail(skill_rail)
    tokens = bind_scoped_skills({"agent_skills_required": ["ambient"]})
    try:
        rail._load_required_skills()
        assert builder.get_section(SectionName.SKILLS).priority == 40
    finally:
        reset_scoped_skills(tokens)
