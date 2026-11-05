"""Test Slack decision input metadata."""

import pytest

from belle_agent.platforms.slack import decision_questions as dq


@pytest.mark.parametrize("chat_type,expected", [
    ("im", "one_to_one_dm"), ("mpim", "group_dm"),
    ("channel", "public_channel"), ("group", "private_channel"),
    ("", "unknown"), ("unexpected", "unknown"),
])
def test_conversation_kind_uses_explicit_type(chat_type, expected):
    state = dq.build_state(
        channel="C001", bot_name="assistant", recent_messages=[],
        last_message={"author": "user", "text": "hello"}, chat_type=chat_type,
    )
    assert state["conversation_kind"] == expected
    assert state["event_kind"] == "message"
    assert state["history_scope"] == "channel"


def test_message_copy_preserves_facts_and_drops_policy_fields():
    entry = {
        "author": "user", "text": "check this", "ts": "1700000001.000001",
        "thread_ts": "1700000000.000001", "file_count": 2,
        "attachment_count": 1, "addressed": True, "trigger": "mention",
        "response_requested": True, "thread_position": "root",
    }
    state = dq.build_state(
        channel="C001", bot_name="assistant", recent_messages=[entry],
        last_message=entry,
    )
    expected = {
        "author": "user", "text": "check this", "ts": "1700000001.000001",
        "thread_ts": "1700000000.000001", "file_count": 2,
        "attachment_count": 1, "thread_position": "reply",
    }
    assert state["last_message"] == expected
    assert state["recent_messages"] == [expected]


@pytest.mark.parametrize("ts,thread_ts,expected", [
    ("1.000001", "", "root"), ("1.000001", "1.000001", "root"),
    ("2.000001", "1.000001", "reply"), ("", "1.000001", "unknown"),
])
def test_thread_position_distinguishes_parent_from_reply(ts, thread_ts, expected):
    entry = dq.message_entry("user", "text", ts=ts, thread_ts=thread_ts)
    assert entry["thread_position"] == expected


def test_log_versions_identify_state_separately_from_questions():
    record = dq.decision_record(
        identity={}, answers=None, outcome="declined", adapter="stub",
        latency_ms=0, window=0,
    )
    assert record["state_version"] == 2
    assert record["question_set_version"] == 2
    assert record["v"] == 1
