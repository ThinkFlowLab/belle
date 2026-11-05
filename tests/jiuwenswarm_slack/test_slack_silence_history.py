"""Silence policy across the JiuwenSwarm stream and history boundary."""

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from belle_agent.silence import REPLY_OPTIONAL_KEY, SILENCE_UNSTORED_KEY
from jiuwenswarm.agents.harness.common.session_ops_service import (
    _build_context_messages_from_history,
)
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module
from jiuwenswarm.server.runtime.session import session_history

pytestmark = pytest.mark.usefixtures("cron_hooks")


@pytest.mark.asyncio
@pytest.mark.parametrize("payload_kind", ["chunk", "dict"])
@pytest.mark.parametrize("empty_final", [False, True])
@pytest.mark.parametrize("licensed", [False, True])
@pytest.mark.parametrize("answer", ["NO_REPLY", "A real answer"])
async def test_silent_final_history_matches_request_license(
    monkeypatch: pytest.MonkeyPatch,
    payload_kind: str,
    empty_final: bool,
    licensed: bool,
    answer: str,
) -> None:
    records: list[dict] = []

    def record_history(**record) -> None:
        if record["role"] == "user" or session_history._has_persistable_assistant_payload(
            content_text=record["content"],
            event_type=record.get("event_type"),
            extra=record.get("extra"),
        ):
            records.append(record)

    class FakeAdapter:
        @staticmethod
        async def process_message_stream_impl(*_args, **_kwargs):
            payloads = (
                [{"event_type": "chat.delta", "content": answer},
                 {"event_type": "chat.final", "content": ""}]
                if empty_final else [{"event_type": "chat.final", "content": answer}]
            )
            for payload in payloads:
                yield (
                    AgentResponseChunk(
                        request_id="req-silence",
                        channel_id="slack",
                        payload=payload,
                        is_complete=False,
                    )
                    if payload_kind == "chunk" else payload
                )

    monkeypatch.setattr(
        interface_module.JiuWenSwarm,
        "_ensure_adapter",
        lambda *_args, **_kwargs: FakeAdapter(),
    )
    monkeypatch.setattr(interface_module, "get_config", lambda: {"memory": {"mode": "disabled"}})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(interface_module, "append_history_record", record_history)

    request = AgentRequest(
        request_id="req-silence",
        channel_id="slack",
        session_id="sess-silence",
        params={"query": "test", "mode": "agent"},
        metadata={REPLY_OPTIONAL_KEY: True, SILENCE_UNSTORED_KEY: True} if licensed else {},
        is_stream=True,
    )
    chunks = [chunk async for chunk in interface_module.JiuWenSwarm().process_message_stream(request)]

    assert any(chunk.payload.get("event_type") == "chat.final" for chunk in chunks if chunk.payload)
    finals = [record for record in records if record.get("event_type") == "chat.final"]
    withheld = licensed and answer == "NO_REPLY"
    assert [record["content"] for record in finals] == ([] if withheld else [answer])
    messages, _ = _build_context_messages_from_history(records)
    assert [message.content for message in messages if type(message).__name__ == "AssistantMessage"] == (
        [] if withheld else [answer]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_final", [False, True])
@pytest.mark.parametrize("licensed", [False, True])
async def test_control_input_silent_final_history(
    monkeypatch: pytest.MonkeyPatch,
    empty_final: bool,
    licensed: bool,
) -> None:
    records: list[dict] = []
    monkeypatch.setattr(interface_module, "append_history_record", lambda **record: records.append(record))
    request = AgentRequest(
        request_id="req-control-silence",
        channel_id="slack",
        session_id="sess-control-silence",
        params={"mode": "agent"},
        metadata={REPLY_OPTIONAL_KEY: True, SILENCE_UNSTORED_KEY: True} if licensed else {},
    )
    swarm = interface_module.JiuWenSwarm()
    pending: list[str] = []
    started: list[float | None] = [None]
    payloads = (
        [{"event_type": "chat.delta", "content": "NO_REPLY"},
         {"event_type": "chat.final", "content": ""}]
        if empty_final else [{"event_type": "chat.final", "content": "NO_REPLY"}]
    )
    for payload in payloads:
        await swarm._persist_control_chunk_to_history(
            AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload=payload,
                is_complete=False,
            ),
            request=request,
            session_id=request.session_id,
            rid=request.request_id,
            cid=request.channel_id,
            pending_final_chunks=pending,
            pending_final_started_at_holder=started,
        )

    assert [record["content"] for record in records if record.get("content")] == (
        [] if licensed else ["NO_REPLY"]
    )
