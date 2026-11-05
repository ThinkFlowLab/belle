"""Shared fixture checks for Slack runtime-tool registration tests."""

from types import SimpleNamespace
from typing import Any

from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_module
from belle_agent.integrations.jiuwenswarm.slack_runtime_tools import SlackRuntimeTools


class SlackRuntimeAbilityManager:
    def __init__(self) -> None:
        self._cards: dict[str, Any] = {}

    def list(self) -> list[Any]:
        return list(self._cards.values())

    def add(self, card: Any) -> None:
        self._cards[card.name] = card

    def get(self, name: str) -> Any:
        return self._cards.get(name)

    def remove(self, name: str) -> None:
        self._cards.pop(name, None)


class SlackRuntimeResourceManager:
    def __init__(self, *, reject_duplicates: bool = False) -> None:
        self.tools: dict[str, Any] = {}
        self.reject_duplicates = reject_duplicates

    def add_tool(self, tool: Any) -> None:
        if self.reject_duplicates and tool.card.id in self.tools:
            raise AssertionError(f"duplicate tool id: {tool.card.id}")
        self.tools[tool.card.id] = tool


def make_slack_runtime_adapter(instance: Any) -> Any:
    """Build an adapter for Slack runtime-tool tests without starting a session."""
    adapter = object.__new__(interface_module.JiuWenSwarmDeepAdapter)
    adapter._instance = instance
    adapter._send_file_toolkit = None
    adapter._session_messaging_toolkit = None
    adapter._last_mode = "agent.work.normal"
    adapter._runtime_cron_tool_context = interface_module._RuntimeCronToolContext(
        tool_scope=f"test_{id(adapter):x}",
    )
    adapter._slack_manager = SlackRuntimeTools(instance, adapter._runtime_cron_tool_context)
    adapter.register_agent_runtime_tools(adapter._slack_manager)
    adapter._cron_tools_registered_language = None
    adapter._build_cron_tools = lambda: []
    adapter._resolve_prompt_channel = lambda _session_id: "web"
    return adapter


async def update_slack_runtime_tools(
    adapter: Any,
    *,
    metadata: dict[str, Any] | None,
    channel_id: str = "slack",
) -> None:
    channel_token = interface_module._CRON_TOOL_CHANNEL_ID.set(channel_id)
    metadata_token = interface_module._CRON_TOOL_METADATA.set(metadata)
    bound_token = interface_module._CRON_TOOL_BOUND.set(True)
    try:
        adapter._runtime_cron_tool_context.remember_current_binding()
        await adapter._update_session_tools(
            session_id="session-1", request_id="request-1", channel_id=channel_id
        )
    finally:
        interface_module._CRON_TOOL_BOUND.reset(bound_token)
        interface_module._CRON_TOOL_METADATA.reset(metadata_token)
        interface_module._CRON_TOOL_CHANNEL_ID.reset(channel_token)


def fake_belle_plugin_loader(resources: Any):
    async def load_plugin(instance: Any, *, rails=(), tools=(), skills=False):
        assert not skills

        async def unload_extension(record):
            for tool in record.tools:
                entries = resources.tools
                if isinstance(entries, dict):
                    entries.pop(tool.card.id, None)
                else:
                    entries.remove(tool)
                instance.ability_manager.remove(tool.card.name)

        if not hasattr(instance, "unload_extension"):
            instance.unload_extension = unload_extension
        for rail in rails:
            await instance.register_rail(rail)
        for tool in tools:
            entries = getattr(resources, "tools", None)
            if isinstance(entries, dict) and tool.card.id in entries:
                entries[tool.card.id] = tool
            else:
                resources.add_tool(tool)
            instance.ability_manager.add(tool.card)
        return SimpleNamespace(tools=tuple(tools), rails=tuple(rails))
    return load_plugin
