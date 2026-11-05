"""Discord posting with explicit destination and visibility checks."""

import json
import secrets
import weakref
from collections.abc import Callable, Mapping
from typing import Any, Literal, NoReturn

from belle_agent.platforms.discord.affordances import MESSAGE_LIMIT
from belle_agent.platforms.discord.client import DiscordError
from belle_agent.platforms.discord.ids import checks_discord_ids, discord_id_error, message_url
from belle_agent.platforms.discord.render import RENDERER
from belle_agent.platforms.discord.rich_post import RichPost, RichPostError, compose_rich_post, rich_tool_properties
from belle_agent.platforms.discord.targets import MENTION_EVERYONE, SEND, SEND_TTS, TargetResolver
from belle_agent.tools.host import active_call, refusal

POST_MESSAGE = "post_message"
FORWARD_MESSAGE = "forward_discord_message"
CROSSPOST_MESSAGE = "crosspost_discord_message"
#: The tool metadata keys, as ``DiscordPlatform.request_metadata`` writes them.
METADATA_CHANNEL = "discord_channel_id"
METADATA_CHANNEL_TYPE = "discord_channel_type"
METADATA_MESSAGE = "discord_message_id"
METADATA_THREAD = "discord_thread_id"
METADATA_REPLY_THREAD = "discord_reply_thread_id"
METADATA_USER = "discord_user_id"
METADATA_GUILD = "discord_guild_id"
THREAD_CREATION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "description": "Creation options for a new thread. A text or announcement channel needs a message ID to start a thread; a forum or media channel creates a post with thread.name and optional available applied_tag_ids.",
    "properties": {
        "name": {
            "type": "string", "minLength": 1, "maxLength": 100,
            "pattern": r"[\s\S]*\S[\s\S]*",
            "description": "Required for a new forum or media post; 1 to 100 characters.",
        },
        "auto_archive_minutes": {"type": "integer", "enum": [60, 1440, 4320, 10080]},
        "rate_limit_per_user": {"type": ["integer", "null"], "minimum": 0, "maximum": 21600},
        "applied_tag_ids": {
            "type": "array", "maxItems": 5, "uniqueItems": True,
            "description": "Up to five distinct tag IDs available in the target forum or media channel. A channel that requires a tag needs at least one.",
            "items": {"type": "string", "pattern": "^[0-9]{17,20}$"},
        },
    },
}


class DiscordToolkit:
    """Post to a resolved Discord destination."""

    def __init__(self, platform: Any, metadata_provider: Callable[[], Mapping[str, Any]]) -> None:
        self._platform = platform
        self._metadata_provider = metadata_provider
        # Weak call ownership removes denied/expired approvals when the host releases them.
        self._audiences: dict[int, tuple[Any, Any, Any]] = {}

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """The tools, as the tool host lists them."""
        return [
            {
                "name": POST_MESSAGE,
                "description": (
                    "Post in a Discord text, announcement, forum or media channel, an existing thread, or a DM, as Discord Markdown. "
                    "A forum or media post creates a thread with its first message. A long text goes in several"
                    " messages. The result names every posted part, its actual location, and the first message URL when known. If a later part"
                    " fails, retrying the whole text can duplicate the parts already posted."
                ),
                "input_params": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Message text. Optional when another sendable field is present."},
                        "chat_id": {
                            "type": "string",
                            "description": "Destination channel, thread, or DM ID. Set this ID to post outside the current conversation. Call this tool to request the post. Belle asks the requester to approve delivery to additional readers before sending. Do not combine with recipient_user_id. Guild audience checks require Server Members access.",
                        },
                        "recipient_user_id": {"type": "string", "description": "Open a DM with this user; mutually exclusive with chat_id."},
                        "reply_to_message_id": {
                            "type": "string",
                            "description": (
                                "A message in the target channel. Required for reply and for starting a thread."
                            ),
                        },
                        "reply_mode": {
                            "type": "string", "enum": ["auto", "top_level", "reply", "thread"], "default": "auto",
                            "description": "auto uses configured source placement; elsewhere replies natively when a message is named. top_level rejects a reply ID and defaults to the source parent channel. reply uses a native reply. thread uses an existing thread, starts one from a message in a text or announcement channel, or creates a forum or media post. Forum and media posts require thread.name. Threads are unavailable in DMs.",
                        },
                        "thread": THREAD_CREATION_SCHEMA,
                        **rich_tool_properties(),
                    },
                },
            },
            {
                "name": FORWARD_MESSAGE,
                "description": "Forward an accessible Discord message as a native snapshot to another accessible channel or DM. Belle asks the requester before forwarding to additional readers.",
                "input_params": {"type": "object", "required": ["source_chat_id", "source_message_id", "chat_id"],
                                 "properties": {
                                     "source_chat_id": {"type": "string", "description": "Channel or DM containing the source message."},
                                     "source_message_id": {"type": "string", "description": "Message to forward."},
                                     "chat_id": {"type": "string", "description": "Destination channel or DM."},
                                 }},
            },
            {
                "name": CROSSPOST_MESSAGE,
                "description": "Crosspost this bot's announcement message to following channels. Followers may include people outside the source server, so Belle asks the requester first.",
                "input_params": {"type": "object", "required": ["message_id"],
                                 "properties": {
                                     "message_id": {"type": "string", "description": "This bot's message in an announcement channel."},
                                     "chat_id": {"type": "string", "description": "Announcement channel; omit for this conversation."},
                                 }},
            },
        ]

    @staticmethod
    def _prepared(arguments: Mapping[str, Any]) -> list[RichPost]:
        text = arguments.get("text")
        if text is not None and (not isinstance(text, str) or not text.strip()):
            raise RichPostError("text", "a nonempty string or omitted when another sendable field is present")
        chunks = [part.payload["content"] for part in RENDERER.parts(text, MESSAGE_LIMIT)] if text else [None]
        fields: dict[str, Any] = {name: arguments.get(name) for name in (
            "embeds", "components", "poll", "sticker_ids", "attachments", "mentions", "shared_client_theme",
        )}
        fields.update({name: arguments.get(name, False) for name in ("suppress_embeds", "suppress_notifications", "tts")})
        first = compose_rich_post(text=chunks[0], allow_role_mentions=True, allow_everyone_mentions=True,
                                  allow_tts=True, **fields)
        later = {name: fields[name] for name in ("mentions", "suppress_embeds", "suppress_notifications", "tts")}
        return [first, *(compose_rich_post(text=chunk, allow_role_mentions=True, allow_everyone_mentions=True,
                                           allow_tts=True, **later) for chunk in chunks[1:])]

    async def _rich_permissions(self, target: str, requester: str, arguments: Mapping[str, Any]) -> None:
        mentions = arguments.get("mentions") or {}
        roles = mentions.get("roles") or []
        everyone = mentions.get("everyone", False)
        tts = arguments.get("tts", False)
        if not (roles or everyone or tts):
            return
        settings = self._platform._settings
        if roles and not settings.allow_role_mentions:
            raise RichPostError("mentions.roles", "allow_role_mentions: true in Belle config")
        if everyone and not settings.allow_everyone_mentions:
            raise RichPostError("mentions.everyone", "allow_everyone_mentions: true in Belle config")
        if tts and not settings.allow_tts:
            raise RichPostError("tts", "allow_tts: true in Belle config")
        if not target:
            if roles or everyone:
                raise RichPostError("mentions", "a guild target for role or everyone pings")
            return
        resolved = await TargetResolver(self._platform).resolve(target, requester)
        if roles and (not resolved.guild or not resolved.bot_permissions & MENTION_EVERYONE
                      and not set(roles) <= resolved.mentionable_roles):
            raise RichPostError("mentions.roles", "mentionable roles or Discord MENTION_EVERYONE permission")
        if everyone and not resolved.bot_permissions & MENTION_EVERYONE:
            raise RichPostError("mentions.everyone", "Discord MENTION_EVERYONE permission")
        if tts and resolved.guild and not resolved.bot_permissions & SEND_TTS:
            raise RichPostError("tts", "Discord SEND_TTS_MESSAGES permission")

    @staticmethod
    def _placement_arguments(
        metadata: Mapping[str, Any], mode: str, reply: str | None,
        thread: dict[str, Any] | None, chat_id: str | None, recipient: str | None,
    ) -> str:
        """Validate placement arguments before resolving or creating a destination."""
        def invalid(detail: str) -> NoReturn:
            raise DiscordError(detail, "POST", "/channels/messages")

        if chat_id and recipient:
            invalid("chat_id_and_recipient_user_id_are_mutually_exclusive")
        if mode not in ("auto", "top_level", "reply", "thread"):
            invalid("reply_mode_expected_auto_top_level_reply_or_thread")
        if mode == "top_level" and reply:
            invalid("reply_to_message_id_conflicts_with_reply_mode_top_level")
        if mode == "reply" and not reply:
            invalid("reply_to_message_id_required_for_reply_mode_reply")
        if thread is not None:
            if not isinstance(thread, dict):
                invalid("thread_expected_object")
            if set(thread) - {"name", "auto_archive_minutes", "rate_limit_per_user", "applied_tag_ids"}:
                invalid("thread_expected_name_auto_archive_minutes_rate_limit_per_user_or_applied_tag_ids")
            if "applied_tag_ids" in thread and reply:
                invalid("thread.applied_tag_ids_requires_new_forum_or_media_post")
            if "name" in thread and (not isinstance(thread["name"], str) or not thread["name"].strip() or len(thread["name"]) > 100):
                invalid("thread.name_expected_1_to_100_characters_containing_non_whitespace")
            if "auto_archive_minutes" in thread and (type(thread["auto_archive_minutes"]) is not int or thread["auto_archive_minutes"] not in (60, 1440, 4320, 10080)):
                invalid("thread.auto_archive_minutes_expected_60_1440_4320_or_10080")
            rate = thread.get("rate_limit_per_user")
            if rate is not None and (type(rate) is not int or not 0 <= rate <= 21600):
                invalid("thread.rate_limit_per_user_expected_null_or_integer_0_to_21600")
        if mode == "auto":
            source = not recipient and (not chat_id or chat_id == metadata.get(METADATA_CHANNEL))
            mode = "thread" if source and metadata.get(METADATA_REPLY_THREAD) else "reply" if reply else "top_level"
        if thread is not None and mode != "thread":
            invalid("thread_requires_reply_mode_thread")
        if mode == "thread":
            if recipient or metadata.get(METADATA_CHANNEL_TYPE) == "dm" and (not chat_id or chat_id == metadata.get(METADATA_CHANNEL)):
                invalid("reply_mode_thread_unsupported_in_dm")
            if not (reply or chat_id or metadata.get(METADATA_THREAD) or metadata.get(METADATA_REPLY_THREAD) or thread is not None):
                invalid("reply_to_message_id_or_existing_thread_required_for_reply_mode_thread")
        return mode

    async def _target(self, metadata: Mapping[str, Any], chat_id: str | None, recipient: str | None, reply: str | None = None, *, at_top_level: bool = False) -> tuple[str, frozenset[str], frozenset[str], str]:
        origin = str(metadata.get(METADATA_CHANNEL) or "")
        requester = str(metadata.get(METADATA_USER) or "")
        if chat_id and recipient:
            raise DiscordError("chat_id_and_recipient_user_id_are_mutually_exclusive", "POST", "/channels/messages")
        resolver = TargetResolver(self._platform)
        source_channel = str(metadata.get(METADATA_THREAD) or origin)
        if recipient:
            readers = frozenset({recipient, self._platform.bot_user_id})
            source = await resolver.resolve(source_channel, requester, posting=False) if source_channel else None
            newcomers = readers - source.readers if source else readers
            if newcomers and not requester:
                raise DiscordError("write_target_widens_audience", "POST", "/users/@me/channels")
            return "", readers, newcomers, "@me"
        target = chat_id or origin
        if not target:
            raise DiscordError("trusted_conversation_context_required", "POST", "/channels/messages")
        if target == origin and (not chat_id and not recipient and not reply and not at_top_level or source_channel == origin):
            scope = str(metadata.get(METADATA_GUILD) or ("@me" if metadata.get(METADATA_CHANNEL_TYPE) == "dm" else ""))
            return target, frozenset(), frozenset(), scope
        audience_target = target
        if reply and source_channel != origin:
            try:
                reply_channel = await self._platform._client.mapping("GET", f"/channels/{reply}")
            except DiscordError as exc:
                if exc.status != 404:
                    raise
            else:
                if reply_channel.get("type") in (10, 11, 12):
                    if str(reply_channel.get("parent_id") or "") != target:
                        raise DiscordError("reply_target_outside_channel", "GET", f"/channels/{reply}")
                    audience_target = reply
        destination = await resolver.resolve(audience_target, requester)
        source = await resolver.resolve(source_channel, requester, posting=False) if source_channel else None
        newcomers = destination.readers - source.readers if source else destination.readers
        if newcomers and not requester:
            raise DiscordError("write_target_widens_audience", "POST", f"/channels/{target}/messages")
        return target, destination.readers, newcomers, destination.guild or ("@me" if destination.kind == 1 else "")

    async def widening_question(self, tool_name: str, arguments: Mapping[str, Any] | None) -> str | None:
        """Ask ToolHost to obtain approval before disclosing to additional readers."""
        if tool_name not in {POST_MESSAGE, FORWARD_MESSAGE, CROSSPOST_MESSAGE} or not isinstance(arguments, Mapping):
            return None
        if discord_id_error(dict(arguments)):
            return None
        metadata = self._metadata_provider()
        if tool_name != POST_MESSAGE:
            try:
                from belle_agent.platforms.discord.writing import DiscordWritingToolkit

                source = DiscordWritingToolkit(self._platform, self._metadata_provider)
                if tool_name == FORWARD_MESSAGE:
                    source_chat = str(arguments.get("source_chat_id") or "")
                    message_id = str(arguments.get("source_message_id") or "")
                    message, _, _ = await source._message_for(source_chat, message_id, write=False)
                    target, readers, newcomers, _ = await self._target(metadata, arguments.get("chat_id"), None)
                    snapshot = (target, readers, newcomers, dict(message), dict(arguments), dict(metadata))
                else:
                    chat_id = str(arguments.get("chat_id") or metadata.get(METADATA_CHANNEL) or "")
                    message_id = str(arguments.get("message_id") or "")
                    message, _, _ = await source._message_for(chat_id, message_id, write=True, extra=SEND)
                    channel = await self._platform._client.mapping("GET", f"/channels/{chat_id}")
                    if channel.get("type") != 5 or str(message.get("author", {}).get("id") or "") != self._platform.bot_user_id:
                        return None
                    snapshot = (chat_id, dict(message), dict(arguments), dict(metadata))
            except DiscordError:
                return None
            context = active_call()
            if context:
                key, call = context
                identity = id(call)
                self._audiences[identity] = (
                    weakref.ref(call, lambda _: self._audiences.pop(identity, None)), key, snapshot,
                )
            if tool_name == CROSSPOST_MESSAGE:
                return f"Crosspost message {message_id} to the announcement channel's followers?"
            if newcomers:
                return f"Forward message {message_id} to {target}, where additional Discord users can read it?"
            return None
        try:
            if any(arguments.get(name) is not None for name in ("text", "embeds", "components", "poll", "sticker_ids", "attachments", "shared_client_theme")):
                self._prepared(arguments)
            mode = self._placement_arguments(metadata, arguments.get("reply_mode", "auto"), arguments.get("reply_to_message_id"), arguments.get("thread"), arguments.get("chat_id"), arguments.get("recipient_user_id"))
            target, readers, newcomers, _ = await self._target(metadata, arguments.get("chat_id"), arguments.get("recipient_user_id"), arguments.get("reply_to_message_id") if mode == "thread" and (arguments.get("chat_id") or not metadata.get(METADATA_THREAD)) else None, at_top_level=arguments.get("reply_mode") == "top_level")
            await self._rich_permissions(target, str(metadata.get(METADATA_USER) or ""), arguments)
        except (DiscordError, RichPostError):
            return None  # The tool reports the same refusal before sending.
        context = active_call()
        if context:
            key, call = context
            identity = id(call)
            self._audiences[identity] = (
                weakref.ref(call, lambda _: self._audiences.pop(identity, None)), key,
                (target, readers, newcomers, dict(arguments), dict(metadata)),
            )
        if newcomers:
            destination = target or f"a DM with {arguments['recipient_user_id']}"
            return f"Post to {destination}, where additional Discord users can read this content?"
        return None

    async def _source_message(self, chat_id: str, message_id: str, *, write: bool, extra: int = 0) -> Mapping[str, Any]:
        from belle_agent.platforms.discord.writing import DiscordWritingToolkit

        source = DiscordWritingToolkit(self._platform, self._metadata_provider)
        message, _, _ = await source._message_for(chat_id, message_id, write=write, extra=extra)
        return message

    @checks_discord_ids
    async def forward_discord_message(self, source_chat_id: str, source_message_id: str, chat_id: str) -> str:
        metadata = self._metadata_provider()
        try:
            message = await self._source_message(source_chat_id, source_message_id, write=False)
            target, readers, newcomers, url_scope = await self._target(metadata, chat_id, None)
            context = active_call()
            saved = self._audiences.pop(id(context[1]), None) if context else None
            snapshot = (target, readers, newcomers, dict(message),
                        {"source_chat_id": source_chat_id, "source_message_id": source_message_id, "chat_id": chat_id},
                        dict(metadata))
            if saved and context:
                reference, key, approved = saved
                if reference() is not context[1] or key != context[0] or approved != snapshot:
                    raise DiscordError("post_audience_changed", "POST", f"/channels/{target}/messages")
            elif newcomers:
                raise DiscordError("post_audience_check_required", "POST", f"/channels/{target}/messages")
            payload = {
                "message_reference": {"type": 1, "channel_id": source_chat_id, "message_id": source_message_id},
                "nonce": str(secrets.randbits(64)), "enforce_nonce": True,
            }
            posted = await self._platform._client.mapping("POST", f"/channels/{target}/messages", json=payload)
            message_id = str(posted.get("id") or "")
            if not message_id or str(posted.get("channel_id") or "") != target:
                raise DiscordError("delivery_uncertain_do_not_retry", "POST", f"/channels/{target}/messages")
            result = {"ok": True, "chat_id": target, "message_id": message_id,
                      "source_chat_id": source_chat_id, "source_message_id": source_message_id}
            if url_scope:
                result["url"] = message_url(url_scope, target, message_id)
            return json.dumps(result)
        except DiscordError as exc:
            return refusal(exc.code, str(exc))

    @checks_discord_ids
    async def crosspost_discord_message(self, message_id: str, chat_id: str | None = None) -> str:
        metadata = self._metadata_provider()
        target = str(chat_id or metadata.get(METADATA_CHANNEL) or "")
        try:
            message = await self._source_message(target, message_id, write=True, extra=SEND)
            channel = await self._platform._client.mapping("GET", f"/channels/{target}")
            if str(channel.get("id") or "") != target or channel.get("type") != 5:
                raise DiscordError("announcement_channel_required", "GET", f"/channels/{target}")
            if str(message.get("author", {}).get("id") or "") != self._platform.bot_user_id:
                raise DiscordError("message_not_own", "GET", f"/channels/{target}/messages/{message_id}")
            context = active_call()
            saved = self._audiences.pop(id(context[1]), None) if context else None
            snapshot = (target, dict(message), {"message_id": message_id, **({"chat_id": chat_id} if chat_id is not None else {})}, dict(metadata))
            if not saved or not context:
                raise DiscordError("crosspost_confirmation_required", "POST", f"/channels/{target}/messages/{message_id}/crosspost")
            reference, key, approved = saved
            if reference() is not context[1] or key != context[0] or approved != snapshot:
                raise DiscordError("post_audience_changed", "POST", f"/channels/{target}/messages/{message_id}/crosspost")
            posted = await self._platform._client.mapping("POST", f"/channels/{target}/messages/{message_id}/crosspost")
            if str(posted.get("id") or "") != message_id or str(posted.get("channel_id") or "") != target:
                raise DiscordError("crosspost_unconfirmed", "POST", f"/channels/{target}/messages/{message_id}/crosspost")
            return json.dumps({"ok": True, "chat_id": target, "message_id": message_id, "crossposted": True})
        except DiscordError as exc:
            return refusal(exc.code, str(exc))

    @checks_discord_ids
    async def post_message(
        self, text: str | None = None, chat_id: str | None = None, reply_to_message_id: str | None = None,
        recipient_user_id: str | None = None,
        reply_mode: Literal["auto", "top_level", "reply", "thread"] = "auto",
        thread: dict[str, Any] | None = None,
        embeds: list[dict[str, Any]] | None = None, components: list[dict[str, Any]] | None = None,
        poll: dict[str, Any] | None = None, sticker_ids: list[str] | None = None,
        attachments: list[dict[str, Any]] | None = None, mentions: dict[str, Any] | None = None,
        suppress_embeds: bool = False, suppress_notifications: bool = False, tts: bool = False,
        shared_client_theme: dict[str, Any] | None = None,
    ) -> str:
        """Post validated content after resolving visibility and placement."""
        metadata = self._metadata_provider()
        sent: list[dict[str, str]] = []
        confirmations: list[Mapping[str, Any]] = []
        target = ""
        try:
            arguments = {"text": text, "embeds": embeds, "components": components, "poll": poll,
                         "sticker_ids": sticker_ids, "attachments": attachments, "mentions": mentions,
                         "suppress_embeds": suppress_embeds, "suppress_notifications": suppress_notifications,
                         "tts": tts, "shared_client_theme": shared_client_theme}
            prepared = self._prepared(arguments)
            mode = self._placement_arguments(metadata, reply_mode, reply_to_message_id, thread, chat_id, recipient_user_id)
            context = active_call()
            saved = self._audiences.pop(id(context[1]), None) if context else None
            target, readers, newcomers, url_scope = await self._target(metadata, chat_id, recipient_user_id, reply_to_message_id if mode == "thread" and (chat_id or not metadata.get(METADATA_THREAD)) else None, at_top_level=reply_mode == "top_level")
            await self._rich_permissions(target, str(metadata.get(METADATA_USER) or ""), arguments)
            source_channel = str(metadata.get(METADATA_THREAD) or metadata.get(METADATA_CHANNEL) or "")
            outside = bool(chat_id and chat_id != source_channel or recipient_user_id
                           or (reply_to_message_id or reply_mode == "top_level") and metadata.get(METADATA_THREAD))
            if saved and context is not None:
                reference, key, snapshot = saved
                if reference() is not context[1] or key != context[0] or snapshot != (
                    target, readers, newcomers, dict(context[1].arguments), dict(metadata),
                ):
                    raise DiscordError("post_audience_changed", "POST", "/channels/messages")
            elif newcomers or outside:
                raise DiscordError("post_audience_check_required", "POST", "/channels/messages")
            if recipient_user_id:
                dm = await self._platform._client.mapping("POST", "/users/@me/channels", json={"recipient_id": recipient_user_id})
                target = str(dm.get("id") or "")
                if not target or dm.get("type") != 1 or {str(user['id']) for user in dm.get('recipients', ())} != {recipient_user_id}:
                    raise DiscordError("target_mismatch", "POST", "/users/@me/channels")
                self._platform._learn_channel(dm)
            origin = str(metadata.get(METADATA_CHANNEL) or "")
            source_thread = str(metadata.get(METADATA_THREAD) or "") if not chat_id and not recipient_user_id else ""
            placement_thread = ""
            if mode == "thread":
                placement_thread = source_thread or reply_to_message_id or (str(metadata.get(METADATA_REPLY_THREAD) or "") if target == origin else "")
                if recipient_user_id or metadata.get(METADATA_CHANNEL_TYPE) == "dm" and target == origin:
                    raise DiscordError("reply_mode_thread_unsupported_in_dm", "POST", f"/channels/{target}/messages")
            elif source_thread and reply_mode != "top_level":
                placement_thread = source_thread
            await self._platform.post_parts(
                target, placement_thread, text or "", sent=sent, prepared_parts=prepared,
                confirmations=confirmations,
                reply_to=reply_to_message_id if mode == "reply" else None,
                thread_options=thread,
                thread_reference=reply_to_message_id if mode == "thread" and source_thread else None,
                validate_placement=True, require_thread=mode == "thread",
                requester=str(metadata.get(METADATA_USER) or ""),
            )
        except RichPostError as exc:
            if exc.field == "text" and not any((embeds, components, poll, sticker_ids, attachments, shared_client_theme)):
                return refusal("nothing_to_post", "text is empty")
            return refusal("invalid_rich_post", str(exc))
        except DiscordError as exc:
            if not sent:
                return refusal(exc.code, str(exc))
            result: dict[str, Any] = {
                "ok": False, "error": exc.code,
                "detail": f"{exc}; {len(sent)} part(s) were posted. Retrying the whole text can duplicate them",
                "chat_id": target, "message_id": sent[0]["message_id"], "thread_id": sent[0]["thread_id"],
                "parts": len(sent), "part_message_ids": [part["message_id"] for part in sent],
                "part_placements": sent,
            }
            if attachments:
                result.update(self._attachment_confirmation(confirmations, len(attachments)))
            return json.dumps(result)
        result = {
            "ok": True, "chat_id": target, "message_id": sent[0]["message_id"],
            "thread_id": sent[0]["thread_id"], "parts": len(sent), "part_placements": sent,
        }
        if len(sent) > 1:
            result["part_message_ids"] = [part["message_id"] for part in sent]
        if url_scope:
            result["url"] = message_url(url_scope, sent[0]["thread_id"] or sent[0]["chat_id"], sent[0]["message_id"])
        if attachments:
            result.update(self._attachment_confirmation(confirmations, len(attachments)))
        return json.dumps(result)

    @staticmethod
    def _attachment_confirmation(confirmations: list[Mapping[str, Any]], expected: int) -> dict[str, Any]:
        returned = confirmations[0].get("attachments") if confirmations else None
        if not isinstance(returned, list) or len(returned) != expected:
            return {"attachment_status": "unconfirmed"}
        ids = [entry.get("id") for entry in returned if isinstance(entry, Mapping)]
        if len(ids) != expected or any(not isinstance(id_, str) or not id_.isdigit() for id_ in ids):
            return {"attachment_status": "unconfirmed"}
        return {"attachment_status": "confirmed", "attachment_ids": ids}
