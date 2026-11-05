import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import BaseError

import jiuwenswarm.common.utils as utils
from belle_agent.scopes import compile_scopes
from belle_agent.integrations.jiuwenswarm.slack_channel import SlackChannelConfig, slack_agent_request_params
from belle_agent.integrations.jiuwenswarm.slack_runtime_tools import SlackRuntimeTools
from belle_agent.platforms.slack.scoped_config import apply_scopes_to_slack_overrides
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime import extension_package_manager as catalog
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

AGENT_TEMPLATES = "agent_templates"
PLUGIN_PACKAGES = "plugin_packages"


def _req(params, *, session_id="s1"):
    return AgentRequest(request_id="r1", channel_id="c1", session_id=session_id, params=params)


def _seed_package(workspace, kind, name, *, version=None):
    package = workspace / "plugins" / kind / "local" / name
    package.mkdir(parents=True, exist_ok=True)
    manifest = {"package_type": "agent_template" if kind == AGENT_TEMPLATES else "plugin"}
    if version is not None:
        manifest["version"] = version
    (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if kind == AGENT_TEMPLATES:
        catalog.upsert_agent_template_marketplace_entry(name, installed=True, source="local")
    else:
        catalog.upsert_plugin_marketplace_entry(name, installed=True, source="local")


@pytest.fixture
def extension_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(utils, "_workspace_base_dir", tmp_path / ".jiuwenswarm")
    monkeypatch.setattr(catalog, "get_equipment_resources_agent_templates_dir", lambda: None)
    monkeypatch.setattr(catalog, "get_equipment_resources_agent_groups_dir", lambda: None)
    monkeypatch.setattr(catalog, "get_equipment_resources_plugin_packages_dir", lambda: None)
    catalog._clear_hub_preview_archives()
    return utils.get_agent_workspace_dir()


@pytest.fixture
def deep_adapter(monkeypatch, extension_workspace):
    adapter = JiuWenSwarmDeepAdapter()
    instance = MagicMock()
    record = MagicMock()
    record.load_id = "rec"
    instance.load_agent_template = AsyncMock(return_value=record)
    instance.load_plugin = AsyncMock(return_value=record)
    instance.unload_extension = AsyncMock(return_value=[])
    monkeypatch.setattr(adapter, "_is_session_live", lambda _: False)
    adapter._instance = instance
    adapter._slack_manager = SlackRuntimeTools(instance, adapter._runtime_cron_tool_context)
    adapter.register_agent_runtime_tools(adapter._slack_manager)
    _seed_package(extension_workspace, AGENT_TEMPLATES, "alpha")
    _seed_package(extension_workspace, PLUGIN_PACKAGES, "beta")
    return adapter


class TestBelleEquipmentIntegration:
    async def test_operator_plugin_claims_existing_slack_tool(self, deep_adapter):
        adapter = deep_adapter
        card = SimpleNamespace(name="read")
        tool = SimpleNamespace(card=card)
        slack_record = SimpleNamespace(load_id="slack")
        abilities = {"read": card}
        adapter._slack_manager._slack_tool_plugin_records = {"read": (slack_record, tool)}
        adapter._instance.ability_manager.get.side_effect = abilities.get

        async def unload(record):
            assert record is slack_record
            abilities.pop("read")
            return []

        async def load_plugin(*_args):
            assert "read" not in abilities
            abilities["read"] = SimpleNamespace(name="read")
            return SimpleNamespace(load_id="operator")

        adapter._instance.unload_extension.side_effect = unload
        adapter._instance.load_plugin.side_effect = load_plugin

        assert await adapter._ensure_chat_extensions(_req({"plugin_names": ["beta"]})) is None
        assert abilities["read"] is not card
        assert adapter._slack_manager._slack_tool_plugin_records == {}

    async def test_failed_operator_mount_restores_slack_tools(self, deep_adapter):
        adapter = deep_adapter
        card = SimpleNamespace(name="read")
        tool = SimpleNamespace(card=card)
        adapter._slack_manager._slack_tool_plugin_records = {
            "read": (SimpleNamespace(load_id="slack"), tool)
        }
        adapter._instance.ability_manager.get.return_value = card
        adapter._instance.load_plugin.side_effect = BaseError(
            StatusCode.DEEPAGENT_LOAD_PLUGIN_ERROR, error_msg="operator load failed"
        )
        adapter._slack_manager._register_slack_tool_cards = AsyncMock()

        response = await adapter._ensure_chat_extensions(_req({"plugin_names": ["beta"]}))

        assert response is not None and not response.ok
        adapter._slack_manager._register_slack_tool_cards.assert_awaited_once_with([tool])

    @pytest.mark.parametrize(
        ("kind", "name", "params", "loader"),
        [
            (PLUGIN_PACKAGES, "beta", {"plugin_names": ["beta"]}, "load_plugin"),
            (AGENT_TEMPLATES, "alpha", {"agent_template_name": "alpha"}, "load_agent_template"),
        ],
    )
    async def test_version_drift_yields_slack_tool_before_reload(
        self, deep_adapter, extension_workspace, kind, name, params, loader
    ):
        adapter = deep_adapter
        assert await adapter._ensure_chat_extensions(_req(params)) is None
        card = SimpleNamespace(name="read")
        tool = SimpleNamespace(card=card)
        slack_record = SimpleNamespace(load_id="slack")
        abilities = {"read": card}
        adapter._slack_manager._slack_tool_plugin_records = {"read": (slack_record, tool)}
        adapter._instance.ability_manager.get.side_effect = abilities.get
        _seed_package(extension_workspace, kind, name, version="2")

        async def unload(record):
            if record is slack_record:
                abilities.pop("read")
            return []

        async def load(*_args):
            assert "read" not in abilities
            abilities["read"] = SimpleNamespace(name="read")
            return SimpleNamespace(load_id="operator")

        adapter._instance.unload_extension.side_effect = unload
        getattr(adapter._instance, loader).side_effect = load

        assert await adapter._ensure_chat_extensions(_req(params)) is None
        assert abilities["read"] is not card
        assert adapter._slack_manager._slack_tool_plugin_records == {}

    async def test_slack_scope_removal_unloads_expert_in_same_session(self, deep_adapter):
        scopes = compile_scopes([
            {"match": {"channel": "slack", "chat": "C1"},
             "agent": {"expert": "alpha"}},
        ], warn=lambda *args: None)
        platform, per_chat = apply_scopes_to_slack_overrides({}, scopes=scopes)
        config = SlackChannelConfig(
            scopes=scopes, platform_override=platform,
            conversation_overrides=per_chat,
        )
        selected = slack_agent_request_params(config, "C1")
        assert selected["agent_template_name"] == "alpha"
        assert await deep_adapter._ensure_chat_extensions(
            _req(selected, session_id="same-slack-session")
        ) is None
        assert deep_adapter._loaded_agent_template[0] == "alpha"

        cleared = slack_agent_request_params(SlackChannelConfig(), "C1")
        assert cleared["agent_template_name"] == ""
        assert await deep_adapter._ensure_chat_extensions(
            _req(cleared, session_id="same-slack-session")
        ) is None
        deep_adapter._instance.unload_extension.assert_awaited_once()
        assert deep_adapter._loaded_agent_template is None
