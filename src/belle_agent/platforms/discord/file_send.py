"""Send inline files or copies of attachments from verified Discord messages."""

import base64
from collections.abc import Callable, Mapping
from typing import Any

from belle_agent.platforms.discord.client import DiscordError
from belle_agent.platforms.discord.ids import SNOWFLAKE_RE, checks_discord_ids
from belle_agent.platforms.discord.reading import DiscordReadingToolkit
from belle_agent.platforms.discord.rich_post import MAX_REQUEST_BYTES
from belle_agent.platforms.discord.toolkit import THREAD_CREATION_SCHEMA, DiscordToolkit
from belle_agent.tools.host import refusal

_ID = {"type": "string", "description": "Discord Snowflake ID."}


class DiscordFileToolkit(DiscordReadingToolkit):
    """Use the post tool's destination and approval checks for file sends."""

    def __init__(self, platform: Any, metadata_provider: Callable[[], Mapping[str, Any]], post: DiscordToolkit) -> None:
        super().__init__(platform, metadata_provider)
        self.post = post

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [{"name": "send_discord_file",
                 "description": "Upload inline content or copy an attachment from a verified Discord message."
                                " The destination and reply options follow post_message. The result reports"
                                " confirmed message and attachment IDs, or states when attachment IDs are unconfirmed.",
                 "input_params": {"type": "object", "properties": {
                     "filename": {"type": "string", "description": "Required for inline content; optional when copying an attachment."},
                     "content": {"type": "string"}, "content_base64": {"type": "string"},
                     "source_attachment": {"type": "object", "required": ["chat_id", "message_id", "attachment_id"],
                                           "additionalProperties": False,
                                           "properties": {"chat_id": _ID, "message_id": _ID, "attachment_id": _ID}},
                     "description": {"type": "string", "maxLength": 1024}, "spoiler": {"type": "boolean"},
                     "caption": {"type": "string"}, "chat_id": _ID, "recipient_user_id": _ID,
                     "reply_to_message_id": _ID,
                     "reply_mode": {"type": "string", "enum": ["auto", "top_level", "reply", "thread"]},
                     "thread": THREAD_CREATION_SCHEMA,
                 }, "required": []}}]

    async def widening_question(self, tool_name: str, arguments: Mapping[str, Any] | None) -> str | None:
        if tool_name != "send_discord_file" or not isinstance(arguments, Mapping):
            return None
        return await self.post.widening_question("post_message", arguments)

    @checks_discord_ids
    async def send_discord_file(
        self, filename: str | None = None, content: str | None = None, content_base64: str | None = None,
        source_attachment: dict[str, str] | None = None, description: str | None = None,
        spoiler: bool = False, caption: str | None = None, chat_id: str | None = None,
        recipient_user_id: str | None = None, reply_to_message_id: str | None = None,
        reply_mode: str = "auto", thread: dict[str, Any] | None = None,
    ) -> str:
        if sum(value is not None for value in (content, content_base64, source_attachment)) != 1:
            return refusal("file_source_invalid", "give exactly one of content, content_base64, or source_attachment")
        if source_attachment is not None and (
            not isinstance(source_attachment, Mapping)
            or set(source_attachment) != {"chat_id", "message_id", "attachment_id"}
            or any(not isinstance(value, str) or not SNOWFLAKE_RE.fullmatch(value)
                   for value in source_attachment.values())
        ):
            return refusal("source_attachment_invalid", "source_attachment needs Discord chat_id, message_id and attachment_id")
        if source_attachment is None and not isinstance(filename, str):
            return refusal("filename_required", "filename is required for inline content")
        try:
            if source_attachment is not None:
                _, requester, _ = self._turn()
                source_chat, source_message, source_file = (
                    source_attachment[name] for name in ("chat_id", "message_id", "attachment_id")
                )
                record, message, _ = await self._one(source_chat, source_message, requester)
                attachment = next((item for item in message.get("attachments", ())
                                   if isinstance(item, Mapping) and str(item.get("id")) == source_file), None)
                if attachment is None:
                    if record["content_availability"] == "may_be_withheld":
                        raise DiscordError("content_unavailable", "GET", f"/channels/{source_chat}/messages/{source_message}")
                    raise DiscordError("attachment_not_in_message", "GET", f"/channels/{source_chat}/messages/{source_message}")
                size = attachment.get("size")
                if type(size) is not int or size < 0:
                    raise DiscordError("bad_response", "GET", f"/channels/{source_chat}/messages/{source_message}")
                if size > MAX_REQUEST_BYTES:
                    return refusal("file_over_size_limit", f"attachment is {size} bytes; Discord limits one request to {MAX_REQUEST_BYTES}")
                filename = filename or str(attachment.get("filename") or "")
                data = await self.platform._client.download(str(attachment.get("url") or ""), MAX_REQUEST_BYTES)
                content_base64 = base64.b64encode(data).decode("ascii")
            file = {"filename": filename, "is_spoiler": spoiler}
            if description is not None:
                file["description"] = description
            if content is not None:
                file["content"] = content
            else:
                file["content_base64"] = content_base64
            return await self.post.post_message(
                text=caption, chat_id=chat_id, recipient_user_id=recipient_user_id,
                reply_to_message_id=reply_to_message_id, reply_mode=reply_mode,
                thread=thread, attachments=[file],
            )
        except DiscordError as exc:
            return refusal(exc.code, str(exc))
