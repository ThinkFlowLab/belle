"""Slack conversation fields exposed to a JiuwenSwarm user turn."""

from belle_agent.integrations.jiuwenswarm.slack_tool_context import (
    METADATA_ORIGIN_KEY,
    ORIGIN_CRON_JOB,
    slack_place_fields,
)


def test_place_contains_only_conversation_and_thread() -> None:
    metadata = {
        "slack_channel_id": "C1",
        "slack_thread_ts": "1710000000.000100",
        "slack_team_id": "T1",
        "slack_user_id": "U1",
        "slack_enforce_audience": True,
    }

    assert slack_place_fields("slack", metadata) == {
        "slack": {"chat_id": "C1", "thread_ts": "1710000000.000100"}
    }
    assert slack_place_fields("__cron__", {**metadata, METADATA_ORIGIN_KEY: ORIGIN_CRON_JOB}) == {
        "slack": {"chat_id": "C1", "thread_ts": "1710000000.000100"}
    }


def test_place_rejects_untrusted_or_missing_conversation() -> None:
    metadata = {"slack_channel_id": "C1"}

    assert slack_place_fields("web", metadata) == {}
    assert slack_place_fields("__cron__", metadata) == {}
    assert slack_place_fields("slack", {"slack_channel_id": " "}) == {}
    assert slack_place_fields("slack", metadata) == {"slack": {"chat_id": "C1"}}
