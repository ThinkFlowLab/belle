"""A JiuwenSwarm job for Mattermost or Matrix: refused, because none of its runs would reach them."""

import json
from types import SimpleNamespace

import pytest

from belle_agent.integrations.jiuwenswarm.job_delivery_rail import (
    CRON_TARGET_UNAVAILABLE,
    HEARTBEAT_UNAVAILABLE,
    RAIL_PRIORITY,
    JobDeliveryRail,
)
from belle_agent.integrations.jiuwenswarm.slack_write_confirmation_rail import (
    RAIL_PRIORITY as CONFIRMATION_PRIORITY,
)

JOB = {"name": "ping", "cron_expr": "0 9 * * *", "timezone": "UTC", "description": "post ping"}

#: The combined tool's job, in the shape a real model gave it: a delivery with no channel.
AT_JOB = {
    "name": "one-time ping", "deleteAfterRun": True, "schedule": {"kind": "at", "at": "2026-10-31T00:02:22+08:00"},
    "payload": {"kind": "systemEvent", "text": "post ping"}, "delivery": {"mode": "announce"},
}

#: A heartbeat follow-up, in the shape a real model gave it after its cron job was refused.
HEARTBEAT = {"name": "one-time ping", "prompt": "Post exactly the text `ping`.", "schedule": {"run_at": 1793376911.0, "type": "once"}}


async def _decide(channel: str, tool_name: str, arguments):
    ctx = SimpleNamespace(inputs=SimpleNamespace(tool_name=tool_name, tool_args=arguments), extra={}, session=None)
    return await JobDeliveryRail(lambda: channel).resolve_interrupt(ctx, None, None)


def _refused(decision) -> dict:
    assert type(decision).__name__ == "RejectResult"
    refused = json.loads(decision.tool_result)
    assert refused["ok"] is False
    return refused


@pytest.mark.parametrize("channel", ["mattermost", "matrix"])
@pytest.mark.parametrize("arguments", [JOB, {**JOB, "targets": ""}, {**JOB, "targets": " Mattermost "}, {**JOB, "targets": "matrix"}])
async def test_a_job_of_these_turns_that_would_deliver_to_them_is_refused(channel, arguments) -> None:
    refused = _refused(await _decide(channel, "cron_create_job", arguments))

    assert refused["error"] == CRON_TARGET_UNAVAILABLE
    assert "nothing was scheduled" in refused["detail"] and "another channel" in refused["detail"]
    assert "heartbeat follow-up cannot post here either" in refused["detail"]

    # Control: the same job named for a channel that delivers jobs is JiuwenSwarm's to take.
    assert type(await _decide(channel, "cron_create_job", {**JOB, "targets": "slack"})).__name__ == "ApproveResult"


async def test_the_refusal_says_what_the_delivery_channel_was_and_where_it_leads() -> None:
    left_out = _refused(await _decide("matrix", "cron_create_job", JOB))["detail"]
    named = _refused(await _decide("matrix", "cron_create_job", {**JOB, "targets": "mattermost"}))["detail"]
    assert left_out.startswith("The job's delivery channel left out means this Matrix conversation")
    assert "cannot deliver a scheduled job to Matrix" in left_out
    assert named.startswith('The job\'s delivery channel "mattermost" names Mattermost')


async def test_a_job_named_for_these_channels_is_refused_from_any_turn() -> None:
    # JiuwenSwarm would deliver it to the turn's own channel instead, without a word.
    detail = _refused(await _decide("slack", "cron_create_job", {**JOB, "targets": "matrix"}))["detail"]
    assert "cannot deliver a scheduled job to Matrix" in detail
    assert "heartbeat" not in detail  # A Slack conversation can have one.

    # Left out in a turn of a channel that delivers jobs, the target is that channel.
    assert type(await _decide("slack", "cron_create_job", JOB)).__name__ == "ApproveResult"


@pytest.mark.parametrize("arguments", [
    {"action": "add", "job": AT_JOB},
    {"action": "add", "job": {**AT_JOB, "delivery": {"channel": "Mattermost"}}},
    {"action": "ADD", "job": {}, **AT_JOB},  # An empty job: the job is read from the flat arguments.
    {"action": "update", "jobId": "j1", "patch": {"delivery": {"channel": ""}}},
    {"action": "update", "jobId": "j1", "targets": "mattermost"},
])
async def test_the_combined_cron_tool_is_refused_alike(arguments) -> None:
    assert _refused(await _decide("mattermost", "cron", arguments))["error"] == CRON_TARGET_UNAVAILABLE


@pytest.mark.parametrize("arguments", [
    {"action": "add", "job": {**AT_JOB, "delivery": {"channel": "slack"}}},
    # JiuwenSwarm reads delivery.channel before targets.
    {"action": "add", "job": {**AT_JOB, "delivery": {"channel": "slack"}, "targets": "mattermost"}},
    {"action": "update", "jobId": "j1", "patch": {"enabled": False}},
    {"action": "list"},
    {"action": "remove", "jobId": "j1"},
])
async def test_the_combined_cron_tool_otherwise_runs(arguments) -> None:
    assert type(await _decide("mattermost", "cron", arguments)).__name__ == "ApproveResult"


async def test_an_update_is_refused_only_when_it_points_the_job_at_these_channels() -> None:
    to_mattermost = {"job_id": "j1", "patch": {"targets": "mattermost"}}
    assert type(await _decide("mattermost", "cron_update_job", to_mattermost)).__name__ == "RejectResult"
    assert type(await _decide("mattermost", "cron_update_job", json.dumps(to_mattermost))).__name__ == "RejectResult"

    # A patch that leaves targets alone keeps the job's own target.
    keep = {"job_id": "j1", "patch": {"enabled": False}}
    assert type(await _decide("mattermost", "cron_update_job", keep)).__name__ == "ApproveResult"


@pytest.mark.parametrize(("channel", "platform"), [("mattermost", "Mattermost"), ("matrix", "Matrix")])
async def test_a_heartbeat_of_these_turns_is_refused(channel, platform) -> None:
    refused = _refused(await _decide(channel, "heartbeat_create_job", HEARTBEAT))

    assert refused["error"] == HEARTBEAT_UNAVAILABLE
    assert f"this {platform} conversation" in refused["detail"]
    assert "nothing was scheduled" in refused["detail"] and "scheduled job cannot post here" in refused["detail"]


async def test_a_heartbeat_of_another_channel_runs() -> None:
    assert type(await _decide("slack", "heartbeat_create_job", HEARTBEAT)).__name__ == "ApproveResult"
    assert type(await _decide("", "heartbeat_create_job", HEARTBEAT)).__name__ == "ApproveResult"


async def test_arguments_that_cannot_be_read_reach_the_tool() -> None:
    assert type(await _decide("mattermost", "cron_create_job", "{not json")).__name__ == "ApproveResult"


def test_the_refusal_comes_before_any_confirmation() -> None:
    assert RAIL_PRIORITY > CONFIRMATION_PRIORITY
