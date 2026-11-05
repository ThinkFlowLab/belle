"""Belle registers its agent plugin through the JiuwenSwarm extension API."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework

from belle_agent.silence import REPLY_OPTIONAL_KEY, SILENCE_UNSTORED_KEY
from belle_agent.integrations.jiuwenswarm.agent_runtime import mount_plugin
from belle_agent.integrations.jiuwenswarm import slack_routing
from belle_agent.integrations.openjiuwen.rails.scoped_skill_use_rail import (
    SCOPED_SKILLS_RUN_CONTEXT_KEY,
)
from belle_agent.scopes.skills import scoped_skill_names, unrestricted_skill_inventory
from jiuwenswarm.extensions.loader import ExtensionLoader
from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.server.runtime.agent_adapter.interface import _suppress_final_record
from jiuwenswarm.server.runtime.agent_adapter.user_turn import UserTurn


@pytest.mark.asyncio
async def test_extension_registers_mount_and_mount_activates(monkeypatch):
    registry = ExtensionRegistry(AsyncCallbackFramework(), {}, None)
    root = Path(__file__).resolve().parents[2] / "src/belle_agent/integrations/jiuwenswarm/extensions/agent_plugin"
    await ExtensionLoader(registry).load_extension(root)
    mounts = registry.get_agent_plugins()
    assert mounts == (mount_plugin,)
    monkeypatch.setattr(ExtensionRegistry, "current_instance", lambda: registry)
    monkeypatch.setattr(ExtensionRegistry, "_instance", registry)
    assert registry.turn_envelope_fields("slack", {"slack_channel_id": "C1"}) == {
        "slack": {"chat_id": "C1"}
    }
    assert "\"slack\": {\"chat_id\": \"C1\"}" in UserTurn(
        text="hello", channel="slack", language="en", files={}, metadata={"slack_channel_id": "C1"}
    ).render()
    request = SimpleNamespace(metadata={REPLY_OPTIONAL_KEY: True, SILENCE_UNSTORED_KEY: True})
    assert _suppress_final_record(request, "NO_REPLY")
    assert not _suppress_final_record(request, "Hello")
    assert not _suppress_final_record(SimpleNamespace(metadata={SILENCE_UNSTORED_KEY: True}), "NO_REPLY")
    inventory = registry.get_skill_inventory()
    assert inventory == (unrestricted_skill_inventory, scoped_skill_names)
    cron_hooks = registry.cron_hooks()["slack"]
    assert cron_hooks.run_metadata is slack_routing.slack_history_metadata_for_cron_job
    agent = SimpleNamespace(_pending_rails=[], _registered_rails=[], active_round=None)
    prepared = registry.prepare_agent_inputs(
        agent, {"agent_skills_available": ["ambient"]}, {"query": "hello"}
    )
    assert prepared["run"]["context"]["extra"][SCOPED_SKILLS_RUN_CONTEXT_KEY] == {
        "agent_skills_available": ["ambient"]
    }

    import belle_agent.integrations.jiuwenswarm.agent_runtime as runtime

    monkeypatch.setattr(runtime, "rails", lambda _agent: (object(),))
    monkeypatch.setattr(runtime, "activate", lambda *_args: activated.append(True))
    activated = []
    specs = []

    async def load(spec):
        specs.append(spec)
        return object()

    services = SimpleNamespace(
        agent=object(),
        load_plugin_spec=load,
        register_request_context=lambda _factory: None,
        register_runtime_tools=lambda _manager: None,
        runtime_context=object(),
    )
    assert await mounts[0](services) is not None
    assert specs[0].rails
    assert activated == [True]


@pytest.mark.asyncio
async def test_the_mount_refuses_a_job_for_the_channel_of_the_request_in_flight(monkeypatch):
    import belle_agent.integrations.jiuwenswarm.agent_runtime as runtime
    from belle_agent.integrations.jiuwenswarm.job_delivery_rail import JobDeliveryRail

    monkeypatch.setattr(runtime, "rails", lambda _agent: ())
    monkeypatch.setattr(runtime, "activate", lambda *_args: None)
    specs = []

    async def load(spec):
        specs.append(spec)
        return object()

    request = SimpleNamespace(channel_id="slack")
    await mount_plugin(SimpleNamespace(
        agent=object(), load_plugin_spec=load, register_request_context=lambda _factory: None,
        register_runtime_tools=lambda _manager: None, runtime_context=request,
    ))
    (rail,) = [entry.params["instance"] for entry in specs[0].rails if isinstance(entry.params["instance"], JobDeliveryRail)]
    ctx = SimpleNamespace(inputs=SimpleNamespace(tool_name="heartbeat_create_job", tool_args={"name": "ping"}))

    assert type(await rail.resolve_interrupt(ctx, None, None)).__name__ == "ApproveResult"
    request.channel_id = "matrix"
    assert type(await rail.resolve_interrupt(ctx, None, None)).__name__ == "RejectResult"
