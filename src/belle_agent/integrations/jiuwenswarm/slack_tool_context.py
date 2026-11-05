# ruff: noqa: F401 - tool context integration exports
"""JiuwenSwarm request provenance for Belle Slack tools."""

from collections.abc import Mapping
from typing import Any

from belle_agent.platforms.slack.history_policy import METADATA_ENFORCE_AUDIENCE_KEY
from belle_agent.platforms.slack.tool_context import (
    SLACK_PERMALINK_RULE,
    SLACK_TS_RULE,
    SlackToolBinding,
    SlackToolRefused,
    cross_installation_refusal,
    runtime_slack_metadata,
    slack_message_id_error,
)

METADATA_ORIGIN_KEY = "slack_history_origin"
ORIGIN_CRON_JOB = "cron_job"


def is_cron_request(metadata: Mapping[str, Any]) -> bool:
    return str(metadata.get(METADATA_ORIGIN_KEY) or "") == ORIGIN_CRON_JOB


def slack_tool_request_metadata(
    channel_id: str | None,
    metadata: Mapping[str, Any] | None,
    *,
    require_conversation: bool = False,
) -> dict[str, Any]:
    channel = str(channel_id or "").strip().lower()
    if channel != "slack":
        if channel != "__cron__":
            return {}
        if not isinstance(metadata, Mapping):
            return {}
        if metadata.get(METADATA_ORIGIN_KEY) != ORIGIN_CRON_JOB:
            return {}
    if not isinstance(metadata, Mapping):
        return {}
    if require_conversation and not str(metadata.get("slack_channel_id") or "").strip():
        return {}
    return dict(metadata)


def slack_place_fields(
    channel_id: str | None,
    metadata: Mapping[str, Any] | None,
) -> dict[str, dict[str, str]]:
    """Expose only a trusted Slack conversation and its optional thread."""
    settled = slack_tool_request_metadata(
        channel_id, metadata, require_conversation=True
    )
    if not settled:
        return {}
    place = {"chat_id": str(settled["slack_channel_id"]).strip()}
    thread_ts = str(settled.get("slack_thread_ts") or "").strip()
    if thread_ts:
        place["thread_ts"] = thread_ts
    return {"slack": place}


def slack_post_request_metadata(channel_id: str | None, metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    metadata = slack_tool_request_metadata(channel_id, metadata, require_conversation=True)
    return metadata if METADATA_ENFORCE_AUDIENCE_KEY in metadata else {}


def slack_pin_request_metadata(channel_id: str | None, metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    return slack_tool_request_metadata(channel_id, metadata, require_conversation=True)


slack_bookmark_request_metadata = slack_pin_request_metadata


def slack_canvas_request_metadata(channel_id: str | None, metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    return slack_tool_request_metadata(channel_id, metadata)


slack_list_request_metadata = slack_canvas_request_metadata


class SlackCronHistoryMixin:
    def _asker_term(self, metadata: Mapping[str, Any]) -> frozenset[str]:
        return frozenset() if is_cron_request(metadata) else super()._asker_term(metadata)


class SlackCronPostMixin:
    def _requester(self, metadata: Mapping[str, Any]) -> str:
        return "" if is_cron_request(metadata) else super()._requester(metadata)
