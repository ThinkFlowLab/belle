import json
from importlib.resources import files
from pathlib import Path

import pytest

pytest.importorskip("openjiuwen")

from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.core.sys_operation import LocalWorkConfig, OperationMode, SysOperationCard
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.rails import SkillUseRail
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.schema.config import DeepAgentConfig
from openjiuwen.harness.workspace.workspace import Workspace

from belle_agent.integrations.openjiuwen.agent_plugin.load import load_plugin, plugin_spec
from belle_agent.integrations.openjiuwen.tools.search import SlackSearchToolkit


class ProbeRail(DeepAgentRail):
    pass


@pytest.mark.asyncio
async def test_agent_plugin_mounts_and_unmounts_skills_rails_and_tools(tmp_path):
    card = SysOperationCard(
        id="belle_plugin_test",
        mode=OperationMode.LOCAL,
        work_config=LocalWorkConfig(work_dir=str(tmp_path)),
    )
    Runner.resource_mgr.add_sys_operation(card)
    agent = DeepAgent(AgentCard(name="Belle plugin test"))
    agent.configure(
        DeepAgentConfig(
            workspace=Workspace(root_path=str(tmp_path / "workspace")),
            sys_operation=Runner.resource_mgr.get_sys_operation(card.id),
            language="en",
            enable_skill_discovery=True,
            rails=[SkillUseRail(skills_dir=[], skill_mode="all", include_tools=False)],
        )
    )
    await agent._ensure_initialized()

    packaged = await agent.load_plugin_spec(plugin_spec())
    assert len([ref for ref in packaged.refs if ref.kind.value == "skill"]) == 12
    await agent.unload_extension(packaged)

    rail = ProbeRail()
    tool = SlackSearchToolkit(metadata_provider=dict).get_tools()[0]
    record = await load_plugin(agent, rails=(rail,), tools=(tool,))
    assert agent.ability_manager.get(tool.card.name) is not None
    assert Runner.resource_mgr.get_tool(tool.card.id) is tool
    assert any(mounted is rail for mounted in agent.find_rails_by_type((ProbeRail,)))
    assert len([ref for ref in record.refs if ref.kind.value == "skill"]) == 12

    await agent.unload_extension(record)
    assert agent.ability_manager.get(tool.card.name) is None
    assert not agent.find_rails_by_type((ProbeRail,))
    skill_rail = agent.find_rails_by_type((SkillUseRail,))[0]
    assert "messaging-context" not in {skill.name for skill in skill_rail.skills}


def test_manifest_is_metadata_only_and_skills_are_shipped():
    manifest = files("belle_agent.integrations.openjiuwen.agent_plugin").joinpath("manifest.json")
    assert manifest.is_file()
    assert "skills" not in json.loads(manifest.read_text())
    skill_specs = plugin_spec().skills
    assert len(skill_specs) == 12
    assert any(Path(skill.dir).name == "session-work" for skill in skill_specs)
    assert all(Path(skill.dir).is_absolute() and (Path(skill.dir) / "SKILL.md").is_file() for skill in skill_specs)
    assert plugin_spec(skills=False).skills == []
