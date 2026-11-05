"""Slack cron sessions grant context only when their origin is proven."""

from types import SimpleNamespace

import pytest

from belle_agent.integrations.jiuwenswarm import slack_routing


@pytest.mark.parametrize(
    ("session", "expected"),
    [
        ("slack_T_C_U", ("C", "")),
        ("slack_T_C_1712345678.000100", ("C", "1712345678.000100")),
        ("slack_T_C_nightly-digest", ("C", "")),
        ("slack_T_C", ("C", "")),
        ("", ("", "")),
        (None, ("", "")),
        ("web_1234", ("", "")),
        ("slack_T", ("", "")),
        ("slack_T_", ("", "")),
        ("slack__U", ("", "")),
    ],
)
def test_parse_cron_session(session, expected):
    assert slack_routing.parse_slack_cron_session(session) == expected


@pytest.mark.parametrize(
    ("session", "channel_type", "thread"),
    [
        ("slack_T_C-ONE_1712345678.000100", "channel", "1712345678.000100"),
        ("slack_T_C-ONE", "channel", None),
        ("slack_T_D-ONE_U-ONE", "im", None),
    ],
)
def test_trusted_job_metadata(monkeypatch, session, channel_type, thread):
    monkeypatch.setattr(slack_routing, "belle_slack_policy", lambda: {"enforce_audience": True})
    job = SimpleNamespace(id="job-1", session_id=session, origin_channel_id="slack")

    metadata = slack_routing.slack_history_metadata_for_cron_job(job)

    assert metadata == {
        "slack_channel_id": session.split("_")[2],
        "slack_channel_type": channel_type,
        "slack_history_origin": "cron_job",
        "slack_enforce_audience": True,
        **({"slack_thread_ts": thread} if thread else {}),
    }
    assert "slack_user_id" not in metadata


def test_cron_metadata_reads_current_policy(monkeypatch):
    policy = {"enforce_audience": False}
    monkeypatch.setattr(slack_routing, "belle_slack_policy", lambda: policy)
    job = SimpleNamespace(id="job-1", session_id="slack_T_C_U", origin_channel_id="slack")

    assert slack_routing.slack_history_metadata_for_cron_job(job)["slack_enforce_audience"] is False
    policy["enforce_audience"] = True
    assert slack_routing.slack_history_metadata_for_cron_job(job)["slack_enforce_audience"] is True


@pytest.mark.parametrize(
    ("session", "origin"),
    [(None, ""), ("tui_1234", ""), ("slack_T_C_U", ""), ("web_42", "slack")],
)
def test_unproven_or_invalid_job_has_no_slack_context(monkeypatch, session, origin):
    monkeypatch.setattr(slack_routing, "belle_slack_policy", lambda: {"enforce_audience": True})
    job = SimpleNamespace(id="job-1", session_id=session, origin_channel_id=origin)
    assert slack_routing.slack_history_metadata_for_cron_job(job) == {}


def test_unreadable_policy_grants_no_cron_context(monkeypatch):
    def unreadable():
        raise OSError("config is unavailable")

    monkeypatch.setattr(slack_routing, "belle_slack_policy", unreadable)
    job = SimpleNamespace(id="job-1", session_id="slack_T_C_U", origin_channel_id="slack")
    assert slack_routing.slack_history_metadata_for_cron_job(job) == {}


def test_cron_origin_marker_is_shared():
    from belle_agent.integrations.jiuwenswarm import slack_tool_context

    assert slack_routing.SLACK_HISTORY_ORIGIN_KEY == slack_tool_context.METADATA_ORIGIN_KEY
    assert slack_routing.SLACK_HISTORY_ORIGIN_CRON_JOB == slack_tool_context.ORIGIN_CRON_JOB
