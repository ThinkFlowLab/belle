"""Slack runtime cards survive an ability-manager reset."""

from types import SimpleNamespace
from typing import Any

import pytest
from belle_agent.integrations.jiuwenswarm import slack_runtime_tools
from belle_agent.integrations.openjiuwen.tools.search import SlackSearchToolkit
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.core.sys_operation import (
    LocalWorkConfig,
    OperationMode,
    SysOperationCard,
)
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.schema.config import DeepAgentConfig
from openjiuwen.harness.workspace.workspace import Workspace

from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from slack_runtime_test_helpers import (
    fake_belle_plugin_loader,
)


class _AbilityManager:
    def __init__(self) -> None:
        self.cards: dict[str, Any] = {}

    def list(self) -> list[Any]:
        return list(self.cards.values())

    def add(self, card: Any) -> None:
        self.cards[card.name] = card

    def get(self, name: str) -> Any:
        return self.cards.get(name)

    def remove(self, name: str) -> None:
        self.cards.pop(name, None)


class _ResourceManager:
    def __init__(self) -> None:
        self.tools: list[Any] = []

    def add_tool(self, tool: Any) -> None:
        self.tools.append(tool)

    def remove_tool(self, tool: Any) -> None:
        self.tools.remove(tool)


@pytest.mark.asyncio
async def test_generic_toolkit_is_reused_with_current_request_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = _ResourceManager()
    ability = _AbilityManager()
    monkeypatch.setattr(slack_runtime_tools, "load_plugin", fake_belle_plugin_loader(resources))
    adapter = slack_runtime_tools.SlackRuntimeTools(SimpleNamespace(ability_manager=ability), None)
    metadata = {}
    created = []

    class Toolkit:
        def __init__(self, metadata_provider):
            self.metadata_provider = metadata_provider
            created.append(self)

        def get_tools(self):
            return [SimpleNamespace(card=SimpleNamespace(name="test_tool"))]

    await adapter._mount_slack_toolkit("test", Toolkit, lambda: metadata)
    assert not created
    metadata = {"channel": "C1"}
    await adapter._mount_slack_toolkit("test", Toolkit, lambda: metadata)
    await adapter._mount_slack_toolkit("test", Toolkit, lambda: metadata)
    assert len(created) == len(resources.tools) == 1
    assert created[0].metadata_provider() == {"channel": "C1"}
    metadata = {"channel": "C2"}
    assert created[0].metadata_provider() == {"channel": "C2"}


@pytest.mark.asyncio
async def test_slack_cards_remount_without_registering_tools_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = _ResourceManager()
    ability = _AbilityManager()
    monkeypatch.setattr(
        slack_runtime_tools, "load_plugin", fake_belle_plugin_loader(resources)
    )
    adapter = object.__new__(slack_runtime_tools.SlackRuntimeTools)
    unloads = []

    async def unload_extension(record):
        unloads.append(record)
        for tool in record.tools:
            resources.remove_tool(tool)
            ability.remove(tool.card.name)

    adapter._instance = SimpleNamespace(ability_manager=ability, unload_extension=unload_extension)
    adapter._slack_tool_plugin_records = {}
    tools = [
        SimpleNamespace(name="read", card=SimpleNamespace(name="read")),
        SimpleNamespace(card=SimpleNamespace(name="write")),
    ]

    await adapter._register_slack_tool_cards(tools)
    assert resources.tools == tools
    assert set(ability.cards) == {"read", "write"}

    await adapter._register_slack_tool_cards(tools)
    assert resources.tools == tools

    ability.cards.pop("write")
    await adapter._register_slack_tool_cards(tools)
    assert resources.tools == tools
    assert set(ability.cards) == {"read", "write"}
    assert len(unloads) == 1
    assert set(adapter._slack_tool_plugin_records) == {"read", "write"}


@pytest.mark.asyncio
async def test_operator_tool_takes_precedence_without_blocking_other_slack_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = _ResourceManager()
    ability = _AbilityManager()
    monkeypatch.setattr(slack_runtime_tools, "load_plugin", fake_belle_plugin_loader(resources))
    operator = SimpleNamespace(name="read")
    ability.add(operator)
    adapter = object.__new__(slack_runtime_tools.SlackRuntimeTools)
    adapter._instance = SimpleNamespace(ability_manager=ability)
    adapter._slack_tool_plugin_records = {}
    read = SimpleNamespace(card=SimpleNamespace(name="read"))
    write = SimpleNamespace(card=SimpleNamespace(name="write"))

    await adapter._register_slack_tool_cards([read, write])
    assert ability.cards["read"] is operator
    assert ability.cards["write"] is write.card
    assert set(adapter._slack_tool_plugin_records) == {"write"}

    ability.remove("write")
    await adapter._register_slack_tool_cards([read, write])
    assert ability.cards["read"] is operator
    assert ability.cards["write"] is write.card


@pytest.mark.asyncio
async def test_hot_operator_handoff_and_stale_record_preserve_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = _ResourceManager()
    ability = _AbilityManager()
    monkeypatch.setattr(slack_runtime_tools, "load_plugin", fake_belle_plugin_loader(resources))
    adapter = object.__new__(slack_runtime_tools.SlackRuntimeTools)
    adapter._instance = SimpleNamespace(ability_manager=ability)
    adapter._slack_tool_plugin_records = {}
    read = SimpleNamespace(card=SimpleNamespace(name="read"))
    write = SimpleNamespace(card=SimpleNamespace(name="write"))
    await adapter._register_slack_tool_cards([read, write])

    assert await adapter._unmount_slack_tool_cards() == [read, write]
    operator = SimpleNamespace(name="read")
    ability.add(operator)
    await adapter._register_slack_tool_cards([read, write])
    assert ability.cards["read"] is operator
    assert ability.cards["write"] is write.card

    ability.remove("write")
    await adapter._register_slack_tool_cards([read, write])
    assert ability.cards["read"] is operator
    assert ability.cards["write"] is write.card

    ability.remove("read")
    await adapter._register_slack_tool_cards([read, write])
    ability.remove("read")
    ability.add(operator)
    ability.remove("write")
    await adapter._register_slack_tool_cards([read, write])
    assert ability.cards["read"] is operator
    assert ability.cards["write"] is write.card
    assert set(adapter._slack_tool_plugin_records) == {"read", "write"}


@pytest.mark.asyncio
async def test_remount_replaces_the_openjiuwen_plugin_record(tmp_path) -> None:
    operation = SysOperationCard(
        id="belle_plugin_tool_remount_test",
        mode=OperationMode.LOCAL,
        work_config=LocalWorkConfig(work_dir=str(tmp_path)),
    )
    Runner.resource_mgr.add_sys_operation(operation)
    agent = DeepAgent(AgentCard(name="Belle plugin tool remount test"))
    agent.configure(
        DeepAgentConfig(
            workspace=Workspace(root_path=str(tmp_path / "workspace")),
            sys_operation=Runner.resource_mgr.get_sys_operation(operation.id),
            language="en",
        )
    )
    await agent._ensure_initialized()
    adapter = object.__new__(slack_runtime_tools.SlackRuntimeTools)
    adapter._instance = agent
    adapter._slack_tool_plugin_records = {}
    tool = SlackSearchToolkit(metadata_provider=dict).get_tools()[0]

    await adapter._register_slack_tool_cards([tool])
    first = next(iter(adapter._slack_tool_plugin_records.values()))[0]
    agent.ability_manager.remove(tool.card.name)
    await adapter._register_slack_tool_cards([tool])

    second = next(iter(adapter._slack_tool_plugin_records.values()))[0]
    assert first.load_id not in agent._load_records
    assert second.load_id in agent._load_records
    assert agent.ability_manager.get(tool.card.name) is not None
