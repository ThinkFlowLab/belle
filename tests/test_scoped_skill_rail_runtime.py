"""Required skills follow OpenJiuwen model, tool, and SDK round callbacks."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio

pytest.importorskip("openjiuwen")

from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent, RunContext
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.prompts import SystemPromptBuilder
from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.harness.rails import SkillUseRail
from openjiuwen.harness.schema.interaction import RoundWorkItem
from openjiuwen.harness.tools import ListSkillTool, SkillTool

from belle_agent.integrations.openjiuwen.rails.scoped_skill_use_rail import (
    SCOPED_SKILLS_RUN_CONTEXT_KEY,
    ScopedSkillUseRail,
    with_scoped_skill_run_context,
)
from belle_agent.scopes.skills import (
    bind_scoped_skills,
    required_skill_text,
    reset_scoped_skills,
    scoped_skill_names,
)


def _skill(root, name, body):
    skill_dir = root / name
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")


@contextmanager
def _scoped_skills(params: dict) -> Iterator[None]:
    tokens = bind_scoped_skills(params)
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


@pytest.mark.asyncio
async def test_required_skill_instructions_are_loaded_before_model_call(skill_rail, required_rail, monkeypatch):
    sections = {}
    skill_rail.system_prompt_builder = SimpleNamespace(
        language="en",
        get_section=lambda name: sections.get(name),
        add_section=lambda section: sections.__setitem__(section.name, section),
    )

    async def native_prompt(rail, _ctx):
        sections[SectionName.SKILLS] = rail._build_skills_section()

    monkeypatch.setattr(SkillUseRail, "_sync_skill_prompt_and_attachment", native_prompt)
    with _scoped_skills(
        {
            "agent_skills_available": ["ambient"],
            "agent_skills_required": ["ambient"],
        }
    ):
        await skill_rail._sync_skill_prompt_and_attachment(None)
        required_rail._load_required_skills()
        native_catalog = skill_rail._build_skills_section().render("en")
        content = sections[SectionName.SKILLS].render("en")
        assert content.startswith(native_catalog + "\n\n# Required Skills\n")
        assert "Follow each instruction on this turn according to its stated conditions." in content
        assert "You do not need `skill_tool` to read this text." in content
        assert "Ambient instructions" in content
        assert "Research instructions" not in content
        await skill_rail._sync_skill_prompt_and_attachment(None)
        required_rail._load_required_skills()
        content = sections[SectionName.SKILLS].render("en")
        assert content.count("# Required Skills") == 1
        assert content.count("## Required Skill: ambient") == 1
        assert content.count("Ambient instructions") == 1
    with _scoped_skills({"agent_skills_available": ["research"]}):
        await skill_rail._sync_skill_prompt_and_attachment(None)
        required_rail._load_required_skills()
        native_catalog = skill_rail._build_skills_section().render("en")
        content = sections[SectionName.SKILLS].render("en")
        assert content == native_catalog
        assert "## Required Skill: ambient" not in content
        assert "research" in content


@pytest.mark.asyncio
async def test_inner_model_and_tool_callbacks_restore_round_scope(skill_rail, required_rail):
    """Restore scoped Skills inside SDK worker tasks."""
    assert {
        AgentCallbackEvent.BEFORE_MODEL_CALL,
        AgentCallbackEvent.AFTER_MODEL_CALL,
        AgentCallbackEvent.ON_MODEL_EXCEPTION,
        AgentCallbackEvent.BEFORE_TOOL_CALL,
        AgentCallbackEvent.AFTER_TOOL_CALL,
        AgentCallbackEvent.ON_TOOL_EXCEPTION,
    } <= required_rail.get_callbacks().keys()
    builder = SystemPromptBuilder(language="en")
    skill_rail.system_prompt_builder = builder
    skill_rail.enabled_skills = {"ambient"}
    await skill_rail.reload_skills()
    run_context = RunContext(
        extra={
            SCOPED_SKILLS_RUN_CONTEXT_KEY: {
                "agent_skills_available": ["ambient"],
                "agent_skills_required": ["ambient"],
            }
        }
    )

    worker_ready = asyncio.Event()
    resume_worker = asyncio.Event()

    async def inner_worker():
        worker_ready.set()
        await resume_worker.wait()
        assert scoped_skill_names() is None
        model_ctx = SimpleNamespace(
            extra={"run_context": run_context},
            inputs=SimpleNamespace(messages=[]),
            session=None,
        )
        await skill_rail.before_model_call(model_ctx)
        await required_rail.before_model_call(model_ctx)
        assert scoped_skill_names() == frozenset({"ambient"})
        assert "Ambient instructions" in required_skill_text()
        assert "Ambient instructions" in builder.build()
        assert "research" not in builder.build()
        await required_rail.after_model_call(model_ctx)
        assert scoped_skill_names() is None
        assert required_skill_text() == ""

        tool_ctx = SimpleNamespace(extra={"run_context": run_context})
        skill_tool = SkillTool(operation=None, get_skills=skill_rail.get_skills_for_session)
        list_tool = ListSkillTool(get_skills=skill_rail.get_skills_for_session)
        await required_rail.before_tool_call(tool_ctx)
        assert (await skill_tool.invoke({"skill_name": "research"})).error == ("Skill not found: research")
        listed = await list_tool.invoke({})
        assert [item["name"] for item in listed.data["skills"]] == ["ambient"]
        await required_rail.after_tool_call(tool_ctx)
        assert scoped_skill_names() is None

        unscoped_ctx = SimpleNamespace(extra={}, inputs=SimpleNamespace(messages=[]), session=None)
        skill_rail.enabled_skills = set()
        await skill_rail.reload_skills()
        await skill_rail.before_model_call(unscoped_ctx)
        await required_rail.before_model_call(unscoped_ctx)
        assert "## Required Skill: ambient" not in builder.build()
        assert "research" in builder.build()
        await required_rail.after_model_call(unscoped_ctx)

    worker = asyncio.create_task(inner_worker())
    await worker_ready.wait()
    with _scoped_skills({"agent_skills_available": ["research"]}):
        resume_worker.set()
        await worker
        assert scoped_skill_names() == frozenset({"research"})


@pytest.mark.asyncio
async def test_copied_tool_context_does_not_reset_parent_scope(required_rail):
    run_context = RunContext(
        extra={SCOPED_SKILLS_RUN_CONTEXT_KEY: {"agent_skills_available": ["ambient"]}}
    )
    parent = SimpleNamespace(extra={"run_context": run_context})
    await required_rail.before_tool_call(parent)
    child = SimpleNamespace(extra=dict(parent.extra))
    await required_rail.before_tool_call(child)
    await required_rail.after_tool_call(child)
    assert scoped_skill_names() == frozenset({"ambient"})

    async def parallel_tool():
        context = SimpleNamespace(extra=dict(parent.extra))
        await required_rail.before_tool_call(context)
        await required_rail.after_tool_call(context)

    await asyncio.gather(parallel_tool(), parallel_tool())
    assert scoped_skill_names() == frozenset({"ambient"})
    await required_rail.after_tool_call(parent)
    assert scoped_skill_names() is None


@pytest.mark.asyncio
async def test_required_disabled_skill_fails_before_model_call(skill_rail, required_rail):
    skill_rail.system_prompt_builder = SimpleNamespace(language="en")

    skill_rail.disabled_skills = {"ambient"}
    await skill_rail.reload_skills()
    with _scoped_skills({"agent_skills_required": ["ambient"]}):
        with pytest.raises(ValueError, match="not installed or enabled: ambient"):
            required_rail._load_required_skills()


@pytest.mark.asyncio
async def test_required_skill_rejects_prompt_mode_none(skill_rail, required_rail):
    skill_rail.system_prompt_builder = SimpleNamespace(mode="none")

    with _scoped_skills({"agent_skills_required": ["ambient"]}):
        with pytest.raises(ValueError, match="prompt_mode=none"):
            required_rail._load_required_skills()


@pytest.mark.asyncio
async def test_two_sdk_rounds_bind_each_turns_skill_scope(skill_rail, required_rail, monkeypatch):
    """Bind each round independently in the SDK supervisor."""
    sections = {}
    skill_rail.system_prompt_builder = SimpleNamespace(
        language="en",
        get_section=lambda name: sections.get(name),
        add_section=lambda section: sections.__setitem__(section.name, section),
    )

    async def no_native_before(_self, _ctx):
        return None

    async def native_prompt(rail, _ctx):
        sections[SectionName.SKILLS] = rail._build_skills_section()

    monkeypatch.setattr(SkillUseRail, "before_invoke", no_native_before)
    monkeypatch.setattr(SkillUseRail, "_sync_skill_prompt_and_attachment", native_prompt)
    observations = []
    list_tool = ListSkillTool(get_skills=skill_rail.get_skills_for_session)

    class CallbackManager:
        async def execute(self, event, ctx):
            if event is AgentCallbackEvent.BEFORE_INVOKE:
                await skill_rail.before_invoke(ctx)
                await required_rail.before_invoke(ctx)
            elif event is AgentCallbackEvent.AFTER_INVOKE:
                await required_rail.after_invoke(ctx)
                await skill_rail.after_invoke(ctx)

    class Controller:
        async def submit_round(self, _session, _query, **_kwargs):
            ctx = SimpleNamespace(extra={"run_context": _kwargs["run_context"]})
            await skill_rail.before_model_call(ctx)
            await required_rail.before_model_call(ctx)
            try:
                listed = await list_tool.invoke({})
                observations.append(
                    (
                        [item["name"] for item in listed.data["skills"]],
                        sections[SectionName.SKILLS].render("en"),
                    )
                )
            finally:
                await required_rail.after_model_call(ctx)

        async def wait_round_completion(self, *, timeout):
            return {}

    controller = Controller()
    coordinator = SimpleNamespace(reset=lambda: None)
    agent = SimpleNamespace(
        agent_callback_manager=CallbackManager(),
        _invoke_active=False,
        _deep_config=None,
        _is_resume_input=lambda _inputs: False,
        _build_interaction_next_work=lambda **_kwargs: None,
        load_state=lambda _session: SimpleNamespace(stop_condition_state=None),
        save_state=lambda *_args: None,
        clear_state=lambda _session: None,
    )

    async def prepare(_session):
        return coordinator, controller

    async def no_op(*_args):
        return None

    agent.prepare_interaction_task_loop = prepare
    agent._normalize_inputs = lambda inputs: DeepAgent._normalize_inputs(agent, inputs)
    agent._sync_expert_role_attachment = no_op
    agent._write_round_result_to_stream = no_op

    for params in (
        {
            "agent_skills_available": ["ambient"],
            "agent_skills_required": ["ambient"],
        },
        {},
    ):
        skill_rail.enabled_skills = set(params.get("agent_skills_available", ()))
        await skill_rail.reload_skills()
        work = RoundWorkItem.user(
            request_id="same-session",
            inputs=with_scoped_skill_run_context({"query": "ping"}, params),
        )
        outcome = await DeepAgent.run_one_round(agent, work, "task", object())
        assert outcome.error_code is None

    assert observations[0][0] == ["ambient"]
    assert "## Required Skill: ambient" in observations[0][1]
    assert set(observations[1][0]) == {"ambient", "research"}
    assert "## Required Skill: ambient" not in observations[1][1]


@pytest.mark.asyncio
async def test_duplicate_reload_callback_restores_outer_scope(required_rail):
    ctx = SimpleNamespace(
        inputs=SimpleNamespace(
            run_context=SimpleNamespace(extra={SCOPED_SKILLS_RUN_CONTEXT_KEY: {"agent_skills_available": ["ambient"]}})
        ),
        extra={},
    )
    with _scoped_skills({"agent_skills_available": ["research"]}):
        await required_rail.before_invoke(ctx)
        await required_rail.before_invoke(ctx)
        assert scoped_skill_names() == frozenset({"ambient"})
        await required_rail.after_invoke(ctx)
        await required_rail.after_invoke(ctx)
        assert scoped_skill_names() == frozenset({"research"})


@pytest.mark.asyncio
async def test_unavailable_skills_leave_the_installed_skills_of_the_round(required_rail):
    carried = with_scoped_skill_run_context({}, {"agent_skills_unavailable": ["ambient"], "other": 1})
    extra = carried["run"]["context"]["extra"]
    assert extra == {SCOPED_SKILLS_RUN_CONTEXT_KEY: {"agent_skills_unavailable": ["ambient"]}}
    ctx = SimpleNamespace(inputs=SimpleNamespace(run_context=SimpleNamespace(extra=extra)), extra={})

    await required_rail.before_invoke(ctx)
    try:
        assert scoped_skill_names() == frozenset({"research"})
    finally:
        await required_rail.after_invoke(ctx)
    assert scoped_skill_names() is None
