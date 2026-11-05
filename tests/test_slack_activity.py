import json

from belle_agent.platforms.slack.activity import (
    ActivityCardRenderer,
    ActivityCardState,
    _SlackActivityRecord,
    _SlackActivityRun,
    compression_event,
    posting_refusal_code,
    todo_snapshot,
)


def test_posting_refusal_reads_error_without_classifying_the_call() -> None:
    assert posting_refusal_code({"tool_result": {"raw_output": '{"error": "not_in_channel"}'}}) == "not_in_channel"
    assert posting_refusal_code({"result": {"error": "channel_not_found"}}) == "channel_not_found"
    assert posting_refusal_code({"result": "invalid"}) == ""


class Renderer(ActivityCardRenderer):
    stop_action_id = "test_stop"


def test_running_card_uses_supplied_stop_action_and_record_ids() -> None:
    record = _SlackActivityRecord("request", "channel", "thread", session_id="session")
    record.subagent_runs["call"] = _SlackActivityRun("call", "research")

    blocks = Renderer._activity_blocks(record)

    assert blocks[0]["tasks"][0]["task_id"] == "subagents-request"
    assert blocks[0]["tasks"][0]["status"] == "in_progress"
    button = blocks[1]["elements"][0]
    assert button["action_id"] == "test_stop"
    assert json.loads(button["value"]) == {
        "session_id": "session",
        "request_id": "request",
        "channel_id": "channel",
    }
    assert len(ActivityCardRenderer._activity_blocks(record)) == 1


def test_settled_record_reports_unreturned_run_and_removes_stop() -> None:
    record = _SlackActivityRecord("request", "channel", "thread", session_id="session")
    record.subagent_runs["call"] = _SlackActivityRun("call", "research")

    record.settle()
    blocks = Renderer._activity_blocks(record)

    assert len(blocks) == 1
    assert blocks[0]["tasks"][0]["status"] == "error"
    assert Renderer._activity_summary(record) == (
        "Turn finished, 1 reported no result - 1 of 1 subagent reported no result"
    )


def test_state_joins_resumed_requests_and_tracks_platform_progress() -> None:
    state = ActivityCardState()
    state.remember_alias("resume-1", "request")
    state.remember_alias("resume-2", "resume-1")
    key = state.key("resume-2", "channel")
    assert key == ("request", "channel")
    record = state.record(key, thread_ts="thread", session_id="session")
    record.note_tool_call("call", "bash")
    record.settle()
    record.note_tool_call("call", "bash")
    assert record.tool_runs["call"].state == "running"
    record.note_compression(
        "pass", "completed",
        {"saved": {"tokens": 40}, "duration_ms": 1000},
    )
    assert record.compression_runs["pass"].saved_tokens == 40
    assert todo_snapshot({"todos": [{"status": "completed", "content": "private"}]}) == {
        "completed": 1,
    }
    assert compression_event({"event_type": "context.compression_state"})
    state.clear()
    assert not state.records and not state.request_aliases


def test_active_cards_are_not_evicted_when_many_turns_run() -> None:
    state = ActivityCardState()
    for index in range(40):
        state.record((f"request-{index}", "channel"), thread_ts="thread", session_id="session")
    assert len(state.records) == 40
