"""Required Skills follow each request, including reused sessions."""

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from jiuwenswarm.agents.harness.common.rails.skill_retrieval_prompt_rail import (
    SkillRetrievalPromptRail,
)
from jiuwenswarm.agents.harness.common.tools.skill_retrieval_toolkits import (
    SkillRetrievalToolkit,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.harness.rails import SkillUseRail

from belle_agent.integrations.jiuwenswarm.agent_runtime import activate
from belle_agent.integrations.jiuwenswarm.agent_runtime import rails as belle_rails
from belle_agent.integrations.jiuwenswarm.skill_prompt import bind_skill_prompt_provider
from belle_agent.integrations.openjiuwen.rails.scoped_skill_use_rail import (
    ScopedSkillUseRail,
)
from belle_agent.scopes.skills import (
    bind_scoped_skills,
    reset_scoped_skills,
    scoped_skill_names,
)


def _skill(root, name, body):
    skill_dir = root / name
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")


@contextmanager
def _scoped_skills(params: dict, installed=None) -> Iterator[None]:
    tokens = bind_scoped_skills(params, installed)
    try:
        yield
    finally:
        reset_scoped_skills(tokens)


@pytest_asyncio.fixture(name="skill_rail")
async def skill_rail_fixture(tmp_path):
    _skill(tmp_path, "ambient", "---\nname: ambient\n---\nAmbient instructions")
    _skill(tmp_path, "research", "---\nname: research\n---\nResearch instructions")
    rail = SkillUseRail(str(tmp_path), skill_mode="all", include_tools=False)
    await rail.reload_skills()
    return rail


@pytest.fixture(name="required_rail")
def required_rail_fixture(skill_rail):
    return ScopedSkillUseRail(skill_rail)


def test_adapter_reloads_native_then_required_skill_rail(tmp_path, monkeypatch):
    adapter = JiuWenSwarmDeepAdapter()
    adapter._second_brain_rail = None
    adapter._skill_manager = SimpleNamespace(list_execution_disabled_skills=lambda: [])
    monkeypatch.setattr(adapter, "_skill_scan_dirs", lambda: [str(tmp_path)])
    monkeypatch.setattr(adapter, "_resolve_skill_mode", lambda *_args, **_kwargs: "all")
    monkeypatch.setattr(
        adapter, "_skill_retrieval_tools_enabled_for_runtime", lambda *_: False
    )
    monkeypatch.setattr(adapter, "_filesystem_rail_enabled_for_profile", lambda: False)
    monkeypatch.setattr(adapter, "_build_heartbeat_rail", lambda: None)
    monkeypatch.setattr(adapter, "_update_permission_rail", lambda *_: None)

    adapter._skill_rail = adapter._build_skill_rail({})
    assert type(adapter._skill_rail) is SkillUseRail
    required = belle_rails(SimpleNamespace(find_rails_by_type=lambda _types: [adapter._skill_rail]))[0]
    assert required._skill_rail is adapter._skill_rail
    assert required.priority == adapter._skill_rail.priority
    adapter._instance = SimpleNamespace(find_rail_by_name=lambda _name: required)
    adapter._agent_plugin_records = [SimpleNamespace(refs=[
        SimpleNamespace(kind=SimpleNamespace(value="rail"), identity="ScopedSkillUseRail")
    ])]
    assert adapter._get_current_agent_rails({}, {})[:2] == [
        adapter._skill_rail,
        required,
    ]


def test_plugin_request_context_validates_and_resets_required_skills():
    contexts = []
    agent = SimpleNamespace(find_rails_by_type=lambda _types: [])
    activate(agent, contexts.append, lambda _tools: None, None)
    request = SimpleNamespace(metadata={}, params={
        "agent_skills_available": ["ambient"],
        "agent_skills_required": ["ambient"],
    })
    with pytest.raises(ValueError, match="active Skill rail"):
        with contexts[0](request):
            pass

    agent.find_rails_by_type = lambda types: (
        [] if SkillRetrievalPromptRail in types else [object()]
    )
    with contexts[0](request):
        assert scoped_skill_names() == frozenset({"ambient"})
    assert scoped_skill_names() is None


@pytest.mark.asyncio
async def test_retrieval_keeps_required_text_after_hiding_catalog(
    skill_rail, required_rail, monkeypatch
):
    sections = {}
    builder = SimpleNamespace(
        language="en",
        get_section=lambda name: sections.get(name),
        add_section=lambda section: sections.__setitem__(section.name, section),
        remove_section=lambda name: sections.pop(name, None),
    )
    skill_rail.system_prompt_builder = builder

    async def native_prompt(rail, _ctx):
        sections[SectionName.SKILLS] = rail._build_skills_section()

    monkeypatch.setattr(
        SkillUseRail, "_sync_skill_prompt_and_attachment", native_prompt
    )
    retrieval = SkillRetrievalPromptRail.__new__(SkillRetrievalPromptRail)
    retrieval._agent = None
    retrieval.system_prompt_builder = builder
    retrieval.attachment_manager = None
    retrieval._session_enabled = True
    retrieval._hidden_skills_section = None
    retrieval._has_skill_index = lambda _ctx: True
    retrieval._hide_legacy_list_skill = lambda: None
    retrieval._filter_legacy_list_skill_from_model_inputs = lambda _ctx: None
    retrieval._prompt_snapshot = lambda *, scoped=False: object()
    retrieval._build_candidate_appendix = lambda _lang, _snapshot: "catalog"
    retrieval._add_prompt_builder_section = lambda _lang, _appendix: None

    async def no_op(_ctx):
        return None

    retrieval._clear_runtime_skill_attachment = no_op
    retrieval._clear_prompt_attachments = no_op
    bind_skill_prompt_provider(
        SimpleNamespace(find_rails_by_type=lambda _types: [retrieval])
    )
    with _scoped_skills({"agent_skills_required": ["ambient"]}):
        await skill_rail._sync_skill_prompt_and_attachment(None)
        required_rail._load_required_skills()
        await retrieval._sync_prompt_attachment(SimpleNamespace(agent=None))
        content = sections[SectionName.SKILLS].render("en")
        assert "Ambient instructions" in content
        assert "research" not in content


def test_retrieval_prompt_provider_binds_new_rails_per_agent():
    full = object()
    restricted = object()

    def new_rail():
        rail = SkillRetrievalPromptRail.__new__(SkillRetrievalPromptRail)
        rail._frozen_prompt_snapshot = full
        rail._prompt_skillfs = SimpleNamespace(prompt_snapshot=lambda: restricted)
        return rail

    current = [new_rail()]
    agent = SimpleNamespace(find_rails_by_type=lambda types: (
        current if SkillRetrievalPromptRail in types else []
    ))
    contexts = []
    activate(agent, contexts.append, lambda _tools: None, None)
    first = current[0]
    assert first._skill_prompt_provider() == (None, "")

    current[0] = new_rail()
    request = SimpleNamespace(metadata={}, params={"agent_skills_available": ["ambient"]})
    with contexts[0](request):
        assert current[0]._skill_prompt_provider()[0] == frozenset({"ambient"})
        assert current[0]._prompt_snapshot(scoped=True) is restricted
        assert first._skill_prompt_provider()[0] == frozenset({"ambient"})
    assert current[0]._skill_prompt_provider() == (None, "")

    unrelated = new_rail()
    assert unrelated._prompt_snapshot() is full


def test_retrieval_index_filters_and_restores_catalog(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    _skill(root, "ambient", "---\nname: ambient\n---\nAmbient")
    _skill(root, "research", "---\nname: research\n---\nResearch")
    toolkit = SkillRetrievalToolkit(
        skill_directories=[str(root)],
        visible_skill_names=scoped_skill_names,
        artifact_root=tmp_path / "index",
    )
    assert toolkit.environment.prompt_snapshot().total_count == 2
    with _scoped_skills({"agent_skills_available": ["ambient"]}):
        assert toolkit.environment.prompt_snapshot().total_count == 1
    with _scoped_skills({"agent_skills_available": []}):
        assert toolkit.environment.prompt_snapshot().total_count == 0
    assert toolkit.environment.prompt_snapshot().total_count == 2


def test_retrieval_index_leaves_out_unavailable_skills(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    _skill(root, "ambient", "---\nname: ambient\n---\nAmbient")
    _skill(root, "research", "---\nname: research\n---\nResearch")
    toolkit = SkillRetrievalToolkit(
        skill_directories=[str(root)],
        visible_skill_names=scoped_skill_names,
        artifact_root=tmp_path / "index",
    )
    with _scoped_skills({"agent_skills_unavailable": ["research"]}, lambda: ["ambient", "research"]):
        assert toolkit.environment.prompt_snapshot().total_count == 1
    assert toolkit.environment.prompt_snapshot().total_count == 2


@pytest.mark.asyncio
async def test_a_request_takes_the_installed_skills_from_the_agent_s_skill_rail(skill_rail):
    agent = SimpleNamespace(find_rails_by_type=lambda types: [skill_rail] if SkillUseRail in types else [])
    contexts = []
    activate(agent, contexts.append, lambda _tools: None, None)

    request = SimpleNamespace(metadata={}, params={"agent_skills_unavailable": ["ambient"]})
    with contexts[0](request):
        assert scoped_skill_names() == frozenset({"research"})
    assert scoped_skill_names() is None
