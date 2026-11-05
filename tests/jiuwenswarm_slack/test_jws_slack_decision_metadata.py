"""Decision inputs preserve message facts and exclude policy conclusions."""

import asyncio

from belle_agent.typed_decision import TypedDecisionClient
from belle_agent.platforms.slack import decision_questions as dq
from test_slack_decision_shadow import (
    ASKER, TS, StubAdapter, body, channel, event, quiet_answers,
)


async def test_connector_supplies_message_facts_to_model():
    adapter = StubAdapter(answers=quiet_answers())
    built, _client, _received, _acknowledged, gate = channel(
        make_gate=lambda window: dq.ShadowDecisionGate(
            TypedDecisionClient(adapter, attempts=1, timeout=1),
            recent_messages=window,
        ),
        history=[{
            "user": ASKER, "text": "earlier", "ts": "1700000000.000001",
            "thread_ts": "1700000000.000001", "files": [{"id": "F001"}],
        }],
    )
    incoming = event()
    incoming.update(channel_type="group", thread_ts="1700000000.000001")
    await built._handle_slack_event(incoming, body(), is_dm=False, trigger="all")
    await asyncio.sleep(0.05)
    await gate.close()
    state = adapter.states[0]
    assert state["conversation_kind"] == "private_channel"
    assert state["last_message"]["ts"] == TS
    assert state["last_message"]["thread_position"] == "reply"
    assert state["last_message"]["file_count"] == 0
    assert state["last_message"]["attachment_count"] == 0
    previous = state["recent_messages"][0]
    assert previous["ts"] == "1700000000.000001"
    assert previous["thread_position"] == "root"
    assert previous["file_count"] == 1
