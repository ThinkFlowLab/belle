"""Scoped tool permissions on a tool call that the DeepAgent scheduler runs.

JiuwenSwarm binds the conversation in the request task, and the DeepAgent runs
tools in the tasks of a scheduler that ``start`` began earlier. These tests run
a real turn that way, with a scripted model.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    build_permission_rail,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import (
    TOOL_PERMISSION_CHANNEL_ID,
    TOOL_PERMISSION_CHAT_ID,
)
from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.tool import tool
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.harness.factory import create_deep_agent
from openjiuwen.harness.schema.interaction import SendInputRequest

from belle_agent.integrations.jiuwenswarm import scope_permissions

sys.path.insert(0, str(Path(__file__).parents[1] / "opencode"))
import recording_proxy  # noqa: E402

TURN_TIMEOUT = 30


@pytest.fixture
def model(tmp_path, monkeypatch):
    """The scripted model, served on a loopback port."""
    monkeypatch.setattr(recording_proxy, "LOG", str(tmp_path / "model-requests.jsonl"))
    recording_proxy._script.clear()
    server = recording_proxy.serve(0, "127.0.0.1")
    yield Model(
        ModelClientConfig(
            client_provider="OpenAI",
            api_key="none",
            api_base=f"http://127.0.0.1:{server.server_address[1]}/v1",
            verify_ssl=False,
        ),
        ModelRequestConfig(model="scripted"),
    )
    server.shutdown()
    server.server_close()
    recording_proxy._script.clear()


@pytest.fixture
def scopes(monkeypatch):
    def install(entries):
        import jiuwenswarm.common.config as config_module

        data = {
            "channels": {},
            "permissions": {"enabled": True, "tools": {}, "defaults": {"*": "allow"}},
        }
        monkeypatch.setattr(config_module, "get_config", lambda: data)
        monkeypatch.setattr(scope_permissions, "load_belle_config", lambda: {"scopes": entries})
        scope_permissions.reset_cache()
        scope_permissions.activate()
        return build_permission_rail({"permissions": data["permissions"]})

    yield install
    scope_permissions.deactivate()
    scope_permissions.reset_cache()


def _tool_results(log: str) -> list[str]:
    results = []
    for line in Path(log).read_text(encoding="utf-8").splitlines():
        for message in (json.loads(line).get("body") or {}).get("messages") or ():
            if message.get("role") == "tool" and message["content"] not in results:
                results.append(message["content"])
    return results


async def _turn(model, rail, *, channel, chat) -> list[tuple[str, str]]:
    """Start the agent unbound, then run one bound turn; return what the tool saw."""
    ran: list[tuple[str, str]] = []

    @tool(name="probe_echo", description="Echo a word.")
    def probe_echo(word: str) -> str:
        ran.append((TOOL_PERMISSION_CHANNEL_ID.get(), TOOL_PERMISSION_CHAT_ID.get()))
        return f"echo {word}"

    agent = create_deep_agent(
        model,
        tools=[probe_echo],
        enable_task_loop=True,
        enable_security_rail=False,
        enable_model_anomaly_detection_rail=False,
    )
    # As the adapter does: the persistent interaction loop never drains queued rails.
    await agent.register_rail(rail)
    session = create_agent_session(session_id="s1", card=agent.card)
    await session.pre_run(inputs={})
    await agent.start(session=session)
    recording_proxy._script.extend([{"calls": [["probe_echo", {"word": "hi"}]]}, {"text": "done"}])
    tokens = (TOOL_PERMISSION_CHANNEL_ID.set(channel), TOOL_PERMISSION_CHAT_ID.set(chat))
    try:
        stream = await agent.attach_output()
        await agent.send_input(SendInputRequest(request_id="r1", inputs={"query": "run the probe"}))

        async def until_answer():
            async for item in stream:
                if getattr(item, "type", None) == "answer":
                    return

        await asyncio.wait_for(until_answer(), TURN_TIMEOUT)
        await stream.close(abort_active_round=False)
    finally:
        TOOL_PERMISSION_CHAT_ID.reset(tokens[1])
        TOOL_PERMISSION_CHANNEL_ID.reset(tokens[0])
        await agent.stop()
    return ran


def _deny_probe(chat):
    return [{"match": {"channel": "slack", "chat": chat}, "permissions": {"tools": {"probe_echo": "deny"}}}]


async def test_a_scoped_deny_holds_on_a_scheduled_tool_call(model, scopes):
    rail = scopes(_deny_probe("C0"))

    ran = await _turn(model, rail, channel="slack", chat="C0")

    assert ran == []
    assert any(result.startswith("[PERMISSION_DENIED]") for result in _tool_results(recording_proxy.LOG))


async def test_a_scheduled_tool_call_runs_when_no_scope_sets_tool_levels(model, scopes):
    rail = scopes([{"match": {"channel": "slack", "chat": "C0"}, "delivery": {"prompt": "Be terse."}}])

    ran = await _turn(model, rail, channel="slack", chat="C0")

    assert len(ran) == 1


@pytest.mark.xfail(
    strict=True,
    reason="JiuwenSwarm binds the conversation in the request task only; the scheduler's tool tasks do not see it",
)
async def test_a_scheduled_tool_call_sees_its_conversation(model, scopes):
    rail = scopes(_deny_probe("C-OTHER"))

    ran = await _turn(model, rail, channel="slack", chat="C0")

    assert ran == [("slack", "C0")]
