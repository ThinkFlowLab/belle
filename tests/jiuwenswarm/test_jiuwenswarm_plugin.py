from pathlib import Path
from types import SimpleNamespace

import pytest
from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.core.sys_operation import (
    LocalWorkConfig,
    OperationMode,
    SysOperationCard,
)
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.rails import SkillUseRail
from openjiuwen.harness.schema.config import DeepAgentConfig
from openjiuwen.harness.workspace.workspace import Workspace

from belle_agent.integrations.jiuwenswarm import agent_runtime
from belle_agent.integrations.jiuwenswarm.job_delivery_rail import JobDeliveryRail
from belle_agent.integrations.openjiuwen.rails.silence_rail import SilenceRail
from belle_agent.silence import REPLY_OPTIONAL_KEY, SILENCE_UNSTORED_KEY
from belle_agent.skills import packaged_skills

#: Every skill Belle packages, by name, in the order the plugin mounts them.
_PACKAGED = [skill["name"] for skill in packaged_skills()]


def _mounted(adapter: JiuWenSwarmDeepAdapter) -> list[str]:
    return [Path(skill.dir).name for skill in adapter._instance.spec.skills]


class _Agent:
    def __init__(self) -> None:
        self.spec = None

    async def load_plugin_spec(self, spec):
        self.spec = spec
        return object()

    def find_rails_by_type(self, _types):
        return []


@pytest.fixture(autouse=True)
def register_belle_plugin(monkeypatch):
    registry = SimpleNamespace(get_agent_plugins=lambda: (agent_runtime.mount_plugin,))
    monkeypatch.setattr(ExtensionRegistry, "current_instance", lambda: registry)


def test_silence_rail_reads_only_the_active_request(monkeypatch):
    monkeypatch.setattr(agent_runtime, "_second_brain_rail", lambda: None)
    rail = agent_runtime.rails(_Agent())[0]
    first = SimpleNamespace(params={}, metadata={REPLY_OPTIONAL_KEY: True, SILENCE_UNSTORED_KEY: True})
    second = SimpleNamespace(params={}, metadata={})

    assert rail._read_metadata() is None
    with agent_runtime._request_scope(_Agent(), first):
        assert rail._read_metadata() == first.metadata
        with agent_runtime._request_scope(_Agent(), second):
            assert rail._read_metadata() == second.metadata
        assert rail._read_metadata() == first.metadata
    assert rail._read_metadata() is None


@pytest.mark.asyncio
async def test_operator_skill_keeps_precedence_when_belle_plugin_loads(monkeypatch):
    # Whether the second brain rail is mounted depends on the Belle file the environment selects.
    monkeypatch.setattr(agent_runtime, "_second_brain_rail", lambda: None)
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._runtime_cron_tool_context = SimpleNamespace(metadata=None, channel_id="", session_id="")
    adapter._instance = _Agent()
    operator_skill = SimpleNamespace(name="messaging-context")
    adapter._skill_rail = SimpleNamespace(skills=[operator_skill])

    await adapter._load_agent_plugins()

    # The operator's skill of the same name stays, so the plugin supplies every other packaged skill.
    assert "messaging-context" in _PACKAGED
    assert _mounted(adapter) == [name for name in _PACKAGED if name != "messaging-context"]
    assert [type(rail.params["instance"]) for rail in adapter._instance.spec.rails] == [SilenceRail, JobDeliveryRail]
    assert adapter._skill_rail.skills == [operator_skill]


@pytest.mark.asyncio
async def test_belle_plugin_supplies_skills_without_operator_collision():
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._runtime_cron_tool_context = SimpleNamespace(metadata=None, channel_id="", session_id="")
    adapter._instance = _Agent()
    adapter._skill_rail = SimpleNamespace(skills=[SimpleNamespace(name="operator-only")])

    await adapter._load_agent_plugins()

    assert _mounted(adapter) == _PACKAGED
    assert adapter._skill_rail.skills[0].name == "operator-only"


@pytest.mark.asyncio
async def test_runtime_policy_activation_follows_plugin_load(monkeypatch):
    from belle_agent.integrations.jiuwenswarm import agent_runtime

    class FailingAgent:
        async def load_plugin_spec(self, _spec):
            raise RuntimeError("load failed")

        def find_rails_by_type(self, _types):
            return []

    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._runtime_cron_tool_context = SimpleNamespace(metadata=None, channel_id="", session_id="")
    adapter._instance = FailingAgent()
    adapter._skill_rail = None
    activated = []
    monkeypatch.setattr(agent_runtime, "activate", lambda *_args: activated.append(True))

    with pytest.raises(RuntimeError, match="load failed"):
        await adapter._load_agent_plugins()
    assert activated == []

    adapter._instance = _Agent()
    await adapter._load_agent_plugins()
    assert activated == [True]


@pytest.mark.asyncio
async def test_belle_skills_survive_agent_reload_and_skill_refresh(tmp_path):
    operation = SysOperationCard(
        id="belle_plugin_reload_test",
        mode=OperationMode.LOCAL,
        work_config=LocalWorkConfig(work_dir=str(tmp_path)),
    )
    Runner.resource_mgr.add_sys_operation(operation)
    skill_rail = SkillUseRail(skills_dir=[], skill_mode="all", include_tools=False)
    agent = DeepAgent(AgentCard(name="Belle plugin reload test"))

    def config():
        return DeepAgentConfig(
            workspace=Workspace(root_path=str(tmp_path / "workspace")),
            sys_operation=Runner.resource_mgr.get_sys_operation(operation.id),
            language="en",
            enable_skill_discovery=True,
            rails=[skill_rail],
        )

    agent.configure(config())
    await agent._ensure_initialized()
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._runtime_cron_tool_context = SimpleNamespace(metadata=None, channel_id="", session_id="")
    adapter._instance = agent
    adapter._skill_rail = skill_rail
    adapter._skill_manager = SimpleNamespace(list_disabled_skills=list)
    adapter._skill_evolution_rail = None
    adapter._skill_scan_dirs = lambda: [str(tmp_path / "host_skills")]

    await adapter._load_agent_plugins()
    assert "messaging-context" in {skill.name for skill in skill_rail.skills}

    refreshed = config()
    adapter._keep_agent_plugin_skills(refreshed)
    agent.configure(refreshed)
    await adapter.refresh_skill_rails()

    assert "messaging-context" in {skill.name for skill in skill_rail.skills}
    assert skill_rail.skills_dir.count(str(tmp_path / "host_skills")) == 1
