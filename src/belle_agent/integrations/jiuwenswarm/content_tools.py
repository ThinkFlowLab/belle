"""Native content tools with JiuwenSwarm's request-scoped Slack file gate."""

import base64
from collections.abc import Mapping
from typing import Any

from belle_agent.integrations.jiuwenswarm.config import load_belle_config
from belle_agent.integrations.jiuwenswarm.slack_history import (
    SlackSessionTeamMixin,
    shared_slack_workspaces,
)
from belle_agent.integrations.jiuwenswarm.slack_routing import filter_slack_history_request_metadata
from belle_agent.integrations.jiuwenswarm.slack_tool_context import SlackCronHistoryMixin
from belle_agent.integrations.openjiuwen.tools.content import content_tools
from belle_agent.platforms.slack.files import SlackInlineHistoryToolkit
from belle_agent.tools.content import content_toolkit_from_config


class _ContentFiles(SlackSessionTeamMixin, SlackCronHistoryMixin, SlackInlineHistoryToolkit):
    """Use native workspace and cron audience rules with path-free file reads."""


class _Attachments:
    def __init__(self, context: Any) -> None:
        self._context = context

    async def resolve_content_attachment(self, ref: str, _metadata: Mapping[str, Any]) -> dict[str, str]:
        """Resolve a Slack file ID using the current request's trusted audience context."""
        metadata = filter_slack_history_request_metadata(self._context.channel_id, self._context.metadata)
        if not metadata:
            raise ValueError("attachment_ref requires a trusted Slack conversation")
        files = _ContentFiles(metadata_provider=lambda: metadata, workspaces=shared_slack_workspaces())
        try:
            record, data = await files.fetch_slack_file(ref)
        finally:
            files._scratch.cleanup()
        if data is None:
            raise ValueError(f"attachment_ref: {record.get('error', 'file_refused')}: {record.get('detail', '')}")
        return {
            "document": base64.b64encode(data).decode("ascii"),
            "document_mime_type": str(record.get("mimetype") or "application/octet-stream"),
        }


def configured_content_tools(context: Any) -> tuple[Any, ...]:
    """Register configured content operations for every native request channel."""
    toolkit = content_toolkit_from_config(load_belle_config(), _Attachments(context))
    return content_tools(toolkit) if toolkit is not None else ()
