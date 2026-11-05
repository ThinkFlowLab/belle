"""The request gate shared by Slack tools."""

from collections.abc import Callable, Mapping
from typing import Any

import pytest

from belle_agent.integrations.jiuwenswarm.slack_bookmarks import (
    slack_bookmark_request_metadata,
)
from belle_agent.integrations.jiuwenswarm.slack_pins import (
    slack_pin_request_metadata,
)
from belle_agent.integrations.jiuwenswarm.slack_tool_context import (
    runtime_slack_metadata,
    slack_tool_request_metadata,
)
from belle_agent.integrations.jiuwenswarm.slack_tool_context import METADATA_ORIGIN_KEY, ORIGIN_CRON_JOB


def test_runtime_metadata_uses_a_copy_without_a_provider() -> None:
    metadata = {"slack_channel_id": "C1"}
    result = runtime_slack_metadata(metadata, None)
    assert result == metadata
    assert result is not metadata


def test_runtime_metadata_reads_provider_once() -> None:
    calls = 0

    def provider() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"slack_channel_id": "C2"}

    assert runtime_slack_metadata({"slack_channel_id": "C1"}, provider) == {
        "slack_channel_id": "C2"
    }
    assert calls == 1


@pytest.mark.parametrize("value", [None, "invalid", 123])
def test_runtime_metadata_rejects_invalid_provider_output(value: Any) -> None:
    assert runtime_slack_metadata({"slack_channel_id": "C1"}, lambda: value) == {}


def test_runtime_metadata_rejects_provider_error() -> None:
    def provider() -> None:
        raise RuntimeError("unavailable")

    assert runtime_slack_metadata({"slack_channel_id": "C1"}, provider) == {}


@pytest.mark.parametrize("require_conversation", [False, True])
@pytest.mark.parametrize(
    ("channel_id", "metadata", "allowed"),
    [
        (" SLACK ", {"slack_channel_id": "C1"}, True),
        (
            "__cron__",
            {"slack_channel_id": "C1", METADATA_ORIGIN_KEY: ORIGIN_CRON_JOB},
            True,
        ),
        ("__cron__", {"slack_channel_id": "C1"}, False),
        ("web", {"slack_channel_id": "C1"}, False),
        ("slack", None, False),
        ("slack", "not metadata", False),
    ],
)
def test_shared_request_gate(
    channel_id: str,
    metadata: Mapping[str, Any] | str | None,
    allowed: bool,
    require_conversation: bool,
) -> None:
    expected = dict(metadata) if allowed else {}
    assert (
        slack_tool_request_metadata(
            channel_id, metadata, require_conversation=require_conversation
        )
        == expected
    )


@pytest.mark.parametrize(
    ("metadata", "require_conversation", "allowed"),
    [
        ({"marker": "x"}, False, True),
        ({"marker": "x"}, True, False),
        ({"slack_channel_id": "  "}, True, False),
        ({"slack_channel_id": "C1"}, True, True),
    ],
)
def test_conversation_requirement(
    metadata: dict[str, Any], require_conversation: bool, allowed: bool
) -> None:
    assert (
        bool(
            slack_tool_request_metadata(
                "slack", metadata, require_conversation=require_conversation
            )
        )
        == allowed
    )


@pytest.mark.parametrize(
    "gate", [slack_pin_request_metadata, slack_bookmark_request_metadata]
)
@pytest.mark.parametrize(
    ("channel_id", "metadata", "allowed"),
    [
        ("slack", {"slack_channel_id": "C1"}, True),
        ("slack", {"slack_channel_id": "C1", "slack_enforce_audience": True}, True),
        (
            "__cron__",
            {"slack_channel_id": "C1", METADATA_ORIGIN_KEY: ORIGIN_CRON_JOB},
            True,
        ),
        ("__cron__", {"slack_channel_id": "C1"}, False),
        ("__cron__", {METADATA_ORIGIN_KEY: ORIGIN_CRON_JOB}, False),
        ("web", {"slack_channel_id": "C1"}, False),
        ("slack", {}, False),
        ("slack", None, False),
    ],
)
def test_conversation_tool_gates(
    gate: Callable[[str | None, Mapping[str, Any] | None], dict[str, Any]],
    channel_id: str,
    metadata: Mapping[str, Any] | None,
    allowed: bool,
) -> None:
    assert gate(channel_id, metadata) == (dict(metadata) if allowed else {})
