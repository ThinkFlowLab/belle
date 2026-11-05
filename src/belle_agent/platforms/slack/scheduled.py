"""Read and manage messages pending in Slack's own scheduling queue."""

import json
import time
from collections.abc import Mapping
from typing import Any

from belle_agent.platforms.slack import blocks as slack_blocks
from belle_agent.platforms.slack.history import SlackHistoryToolkit
from belle_agent.platforms.slack.history_policy import METADATA_TEAM_KEY, SlackWorkspaceUnresolved
from belle_agent.platforms.slack.post import POST_MESSAGE, SlackPostToolkit, _native_content_error
from belle_agent.platforms.slack.text import MAX_SLACK_POST_FALLBACK_LENGTH
from belle_agent.platforms.slack.tool_common import _SlackCallFailure
from belle_agent.platforms.slack.tool_context import SlackToolRefused, checks_slack_ids

_SCHEDULE = "chat_scheduleMessage"
_LIST = "chat_scheduledMessages_list"
_CANCEL = "chat_deleteScheduledMessage"
_WINDOW_SECONDS = 120 * 24 * 60 * 60


def _error(code: str, detail: str, **fields: Any) -> str:
    return json.dumps({"ok": False, "error": code, "detail": detail, **fields})


class SlackScheduledToolkit(SlackPostToolkit):
    """Schedule, list, and cancel messages owned by the bound Slack token."""

    active_turn_only = frozenset({"schedule_slack_message", "cancel_slack_scheduled_message"})

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        target = {"type": "string", "description": "Slack conversation ID; omit for this conversation."}
        return [
            {
                "name": "schedule_slack_message",
                "description": "Schedule one Slack message, at most 120 days ahead. This is Slack's queue, separate from Belle jobs. Slack does not deliver scheduled messages containing metadata, so this tool has no metadata field. A different audience requires requester approval.",
                "input_params": {"type": "object", "required": ["post_at"], "properties": {
                    "post_at": {"type": "integer", "description": "Future Unix time in seconds, up to 120 days ahead."},
                    "chat_id": target,
                    "text": {"type": "string", "description": "Nonempty message text, or fallback when blocks are given."},
                    "blocks": {"type": "array", "items": {"type": "object"}, "description": "Noninteractive Block Kit content."},
                    "attachments": {"type": "array", "items": {"type": "object"}},
                    "markdown_text": {"type": "string", "description": "Native Slack markdown; cannot be combined with text or blocks."},
                    "reply_to_message_id": {"type": "string", "description": "Parent message ID for a thread reply."},
                    "also_post_to_chat": {"type": "boolean", "description": "Broadcast a scheduled thread reply to the channel."},
                    "parse": {"type": "string", "enum": ["none", "full"]},
                    "link_names": {"type": "boolean"},
                    "unfurl_links": {"type": "boolean"},
                    "unfurl_media": {"type": "boolean"},
                }},
            },
            {
                "name": "list_slack_scheduled_messages",
                "description": "List one page of messages scheduled by this app token in an authorized conversation. Slack does not return messages scheduled in its UI. Follow coverage.next_cursor to read every page.",
                "input_params": {"type": "object", "properties": {
                    "chat_id": target,
                    "cursor": {"type": "string", "description": "Next cursor from the previous page."},
                    "limit": {"type": "integer", "minimum": 1},
                    "oldest": {"type": "integer", "description": "Earliest scheduled Unix time, inclusive."},
                    "latest": {"type": "integer", "description": "Latest scheduled Unix time, inclusive."},
                }},
            },
            {
                "name": "cancel_slack_scheduled_message",
                "description": "Cancel a pending message scheduled by this app token. The requester must approve this action during the turn. Slack refuses cancellation during the last 60 seconds before delivery.",
                "input_params": {"type": "object", "required": ["scheduled_message_id"], "properties": {
                    "scheduled_message_id": {"type": "string", "description": "Opaque ID returned by schedule_slack_message or list_slack_scheduled_messages."},
                    "chat_id": target,
                }},
            },
        ]

    async def widening_question(self, tool_name: str, arguments: Mapping[str, Any] | None) -> str | None:
        if not isinstance(arguments, Mapping):
            return None
        if tool_name == "schedule_slack_message":
            return await super().widening_question(POST_MESSAGE, arguments)
        if tool_name == "cancel_slack_scheduled_message" and arguments.get("scheduled_message_id"):
            try:
                origin = self._origin(self._runtime_metadata())
            except SlackToolRefused:
                return None
            channel = str(arguments.get("chat_id") or origin)
            message_id = str(arguments["scheduled_message_id"])
            return f"Cancel scheduled Slack message {message_id} in conversation {channel}?"
        return None

    async def _target_for(self, chat_id: str | None, *, write: bool) -> tuple[dict[str, Any], str, str]:
        metadata = self._runtime_metadata()
        enforced = self._enforce_audience(metadata)
        origin = self._origin(metadata)
        await self._load_settings(metadata)
        target = str(chat_id or "").strip() or origin
        if write:
            target = await self._resolve_target(metadata, chat_id, enforced, origin, may_widen=True)
        elif target != origin:
            gate = SlackHistoryToolkit(metadata=metadata, client=self._client, workspaces=self._workspaces)
            await gate.authorize_read_target(metadata, target)
        return metadata, origin, target

    @checks_slack_ids
    async def schedule_slack_message(
        self, post_at: int, chat_id: str | None = None, text: str | None = None,
        blocks: Any = None, attachments: Any = None, markdown_text: str | None = None,
        reply_to_message_id: str | None = None, also_post_to_chat: bool = False,
        parse: str | None = None, link_names: bool | None = None,
        unfurl_links: bool | None = None, unfurl_media: bool | None = None,
    ) -> str:
        if type(post_at) is not int:
            return _error("post_at_invalid", "post_at must be a Unix timestamp in whole seconds")
        now = int(time.time())
        if not now < post_at <= now + _WINDOW_SECONDS:
            return _error("post_at_out_of_range", "post_at must be in the future and no more than 120 days ahead")
        if text is not None and not isinstance(text, str):
            return _error("text_invalid", "text must be a string")
        if problem := _native_content_error(
            markdown_text=markdown_text, text=text, blocks=blocks, attachments=attachments,
            metadata=None, parse=parse, mrkdwn=None, link_names=link_names,
        ):
            return _error(*problem)
        declared = slack_blocks.apply_block_policy(blocks)
        if blocks is not None and declared is None:
            return _error("blocks_refused", "blocks must be valid, noninteractive Block Kit content")
        if not (str(text or "").strip() or declared or attachments or markdown_text):
            return _error("nothing_to_schedule", "provide nonempty text, blocks, attachments, or markdown_text")
        if text and len(text) > MAX_SLACK_POST_FALLBACK_LENGTH:
            return _error("text_too_long", f"text has {len(text)} characters; one scheduled message accepts at most {MAX_SLACK_POST_FALLBACK_LENGTH}")
        if also_post_to_chat and not reply_to_message_id:
            return _error("thread_required", "also_post_to_chat requires reply_to_message_id")
        try:
            _, _, target = await self._target_for(chat_id, write=True)
            body: dict[str, Any] = {"channel": target, "post_at": post_at}
            for key, value in (
                ("text", text), ("blocks", declared), ("attachments", attachments),
                ("markdown_text", markdown_text), ("parse", parse), ("link_names", link_names),
                ("unfurl_links", unfurl_links), ("unfurl_media", unfurl_media),
            ):
                if value is not None:
                    body[key] = value
            if reply_to_message_id:
                body["thread_ts"] = reply_to_message_id
                if also_post_to_chat:
                    body["reply_broadcast"] = True
            result = await self._call(_SCHEDULE, **body)
            message_id = str(result.get("scheduled_message_id") or "")
            if not message_id or str(result.get("channel") or target) != target:
                return _error("delivery_uncertain_do_not_retry", "Slack did not confirm the scheduled message ID; check the queue before retrying", chat_id=target)
            return json.dumps({"ok": True, "chat_id": target, "scheduled_message_id": message_id,
                               "post_at": int(result.get("post_at") or post_at)})
        except (SlackToolRefused, SlackWorkspaceUnresolved, _SlackCallFailure) as exc:
            return _error(exc.code, getattr(exc, "detail", "") or str(exc))

    @checks_slack_ids
    async def list_slack_scheduled_messages(
        self, chat_id: str | None = None, cursor: str | None = None,
        limit: int | None = None, oldest: int | None = None, latest: int | None = None,
    ) -> str:
        if limit is not None and (type(limit) is not int or limit < 1):
            return _error("limit_invalid", "limit must be a positive integer")
        if any(value is not None and type(value) is not int for value in (oldest, latest)):
            return _error("time_range_invalid", "oldest and latest must be Unix timestamps in whole seconds")
        if oldest is not None and latest is not None and oldest >= latest:
            return _error("time_range_invalid", "oldest must be before latest")
        if cursor is not None and not isinstance(cursor, str):
            return _error("cursor_invalid", "cursor must be a string returned by the previous page")
        try:
            metadata, _, target = await self._target_for(chat_id, write=False)
            body: dict[str, Any] = {"channel": target}
            if team_id := str(metadata.get(METADATA_TEAM_KEY) or ""):
                body["team_id"] = team_id
            for key, value in (("cursor", cursor), ("limit", limit), ("oldest", oldest), ("latest", latest)):
                if value is not None:
                    body[key] = value
            result = await self._call(_LIST, **body)
            rows = result.get("scheduled_messages")
            if not isinstance(rows, list):
                return _error("invalid_response", "Slack returned no scheduled_messages list", chat_id=target)
            records = [dict(item) for item in rows if isinstance(item, Mapping) and item.get("channel_id") == target]
            if len(records) != len(rows):
                return _error("invalid_response", "Slack returned a scheduled message outside the requested conversation", chat_id=target)
            page = result.get("response_metadata")
            if not isinstance(page, Mapping):
                return _error("invalid_response", "Slack returned no pagination metadata", chat_id=target)
            next_cursor = str(page.get("next_cursor") or "")
            return json.dumps({"ok": True, "chat_id": target, "messages": records,
                               "coverage": {"complete": not bool(next_cursor), "next_cursor": next_cursor}})
        except (SlackToolRefused, SlackWorkspaceUnresolved, _SlackCallFailure) as exc:
            return _error(exc.code, getattr(exc, "detail", "") or str(exc))

    @checks_slack_ids
    async def cancel_slack_scheduled_message(self, scheduled_message_id: str, chat_id: str | None = None) -> str:
        if not isinstance(scheduled_message_id, str) or not scheduled_message_id.strip():
            return _error("scheduled_message_id_invalid", "scheduled_message_id must be a nonempty ID from Slack")
        try:
            _, _, target = await self._target_for(chat_id, write=False)
            await self._call(_CANCEL, channel=target, scheduled_message_id=scheduled_message_id.strip())
            return json.dumps({"ok": True, "chat_id": target, "scheduled_message_id": scheduled_message_id.strip(), "cancelled": True})
        except (SlackToolRefused, SlackWorkspaceUnresolved, _SlackCallFailure) as exc:
            return _error(exc.code, getattr(exc, "detail", "") or str(exc))
