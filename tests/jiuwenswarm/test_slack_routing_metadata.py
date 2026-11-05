"""Trusted request metadata for Slack runtime tools."""

from types import MappingProxyType

import pytest

from belle_agent.integrations.jiuwenswarm.slack_routing import (
    SLACK_HISTORY_ORIGIN_CRON_JOB,
    SLACK_HISTORY_ORIGIN_KEY,
    filter_slack_history_request_metadata,
    request_carries_slack_history_context,
)
from belle_agent.platforms.slack.history_policy import METADATA_ENFORCE_AUDIENCE_KEY


@pytest.mark.parametrize(
    ("channel", "origin", "trusted"),
    [
        ("slack", None, True),
        ("__cron__", SLACK_HISTORY_ORIGIN_CRON_JOB, True),
        ("__cron__", None, False),
        ("web", SLACK_HISTORY_ORIGIN_CRON_JOB, False),
    ],
)
def test_request_origin_controls_slack_metadata(
    channel: str, origin: str | None, trusted: bool,
) -> None:
    metadata = {
        "slack_channel_id": "C1",
        METADATA_ENFORCE_AUDIENCE_KEY: False,
        SLACK_HISTORY_ORIGIN_KEY: origin,
    }

    assert request_carries_slack_history_context(channel, metadata) is trusted
    filtered = filter_slack_history_request_metadata(channel, metadata)
    assert filtered == (metadata if trusted else {})
    if trusted:
        assert filtered is not metadata


@pytest.mark.parametrize(
    "metadata",
    [None, {}, {"slack_channel_id": "C1"}, {METADATA_ENFORCE_AUDIENCE_KEY: True}],
)
def test_history_requires_conversation_and_audience_decision(
    metadata: dict[str, object] | None,
) -> None:
    assert filter_slack_history_request_metadata("slack", metadata) == {}


def test_history_rejects_non_dict_metadata_even_with_audience_marker() -> None:
    metadata = MappingProxyType({"slack_channel_id": "C1", METADATA_ENFORCE_AUDIENCE_KEY: False})
    assert filter_slack_history_request_metadata("slack", metadata) == {}
