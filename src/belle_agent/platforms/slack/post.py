"""Post, edit, and delete Slack messages under the audience rule."""

import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from belle_agent.audience import (
    EPHEMERAL_LEAVES_THIS_CONVERSATION,
    Audience,
    audience_newcomers,
    direct_message_question,
    ephemeral_refusal,
    external_question,
    unattended_write_refusal,
    widening_question,
    workspace_question,
    write_permitted,
)
from belle_agent.platforms.slack import blocks as slack_blocks
from belle_agent.platforms.slack.history_policy import (
    METADATA_ENFORCE_AUDIENCE_KEY,
    METADATA_TEAM_KEY,
    METADATA_WRITE_TARGET_REQUIRED_KEY,
    SlackWorkspaceUnresolved,
    effective_audience,
    slack_workspace_blocks,
)
from belle_agent.platforms.slack.json_toolkit import (
    SlackMethodToolkitBase,
    slack_failure_detail,
    slack_refusal_json,
)
from belle_agent.platforms.slack.text import (
    MAX_SLACK_POST_FALLBACK_LENGTH,
    MAX_SLACK_UPDATE_TEXT_LENGTH,
    normalize_slack_mrkdwn,
    split_text,
)
from belle_agent.platforms.slack.tool_common import (
    IncompleteMembers,
    _cache_room,
    _SlackCallFailure,
    conversation_info,
    conversation_members,
)
from belle_agent.platforms.slack.tool_context import (
    SlackToolRefused,
    checks_slack_ids,
    cross_installation_refusal,
    slack_message_id_error,
)

logger = logging.getLogger(__name__)

#: Tool names in registration order.
POST_MESSAGE = "post_message"
EDIT_MESSAGE = "edit_message"
DELETE_MESSAGE = "delete_message"
WRITE_TOOL_NAMES: tuple[str, ...] = (POST_MESSAGE, EDIT_MESSAGE, DELETE_MESSAGE)

#: Posting and editing can widen an audience; deleting cannot.
WIDENING_TOOL_NAMES: frozenset[str] = frozenset({POST_MESSAGE, EDIT_MESSAGE})

#: Accept known and future Slack ID prefixes, but only alphanumeric IDs.
_SLACK_ID_RE = re.compile(r"\A[A-Z][A-Z0-9]{1,32}\Z")

#: ``W`` is an Enterprise Grid user ID.
_USER_ID_PREFIXES = ("U", "W")

_MEMBERS_PAGE_LIMIT = 200

#: Membership cache lifetime in seconds.
_MEMBERS_CACHE_SECONDS = 60.0
_MAX_MEMBERS_CACHE_ENTRIES = 64

#: Lifetime and bound of the asker's name and face, in seconds and entries.
#: Short, because a profile is a thing somebody changes; long enough that a turn
#: posting several times for one person reads the directory once.
_IDENTITY_CACHE_SECONDS = 300.0
_MAX_IDENTITY_CACHE_ENTRIES = 64

#: What an identity override costs an installation, beyond ``chat:write``.
_CUSTOMIZE_SCOPE = "chat:write.customize"

#: Actionable details for Slack refusal codes.
_WRITE_FAILURE_DETAIL: Mapping[str, str] = {
    "channel_not_found": (
        "this workspace has no conversation with that id, or this app is not in"
        " it. A conversation id names a room in one Slack workspace and nothing"
        " at all in another, so an id from somewhere else names nothing here;"
        " find the conversation with find_by_name rather than reusing an id"
    ),
    "not_in_channel": (
        "this app is not a member of that conversation and Slack will not let it"
        " post there. Somebody in the conversation has to invite it; there is no"
        " wording that gets past this"
    ),
    "is_archived": ("that conversation is archived, so nothing can be posted into it until somebody unarchives it"),
    "message_not_found": (
        "that conversation holds no message with that id. A message id is not"
        " transferable between conversations, so one taken from elsewhere names"
        " nothing here"
    ),
    "cant_update_message": (
        "Slack will not let this app change that message. Only a message this"
        " app posted can be edited, and an ephemeral message cannot be edited at"
        " all"
    ),
    "cant_delete_message": (
        "Slack will not let this app delete that message. Only a message this"
        " app posted can be deleted, and an ephemeral message cannot be deleted"
        " at all"
    ),
    "user_not_found": ("this workspace has no user with that id, so no direct message can be opened with them"),
    "user_not_visible": ("this app cannot see that user, so no direct message can be opened with them"),
    "msg_too_long": (
        "Slack refused the text as too long for the call that was made. A fresh"
        " post is split automatically; an edit is not, because Slack accepts"
        " only a tenth as much text in an edit and the rest has nowhere to go"
    ),
    "invalid_response": "Slack returned no valid message timestamp; delivery could not be confirmed",
}

#: Scope names depend on the refused method.
_SCOPE_FOR_METHOD: Mapping[str, str] = {
    "chat_postMessage": "chat:write",
    "chat_postEphemeral": "chat:write",
    "chat_update": "chat:write",
    "chat_delete": "chat:write",
    "conversations_open": "im:write, and mpim:write for a group direct message",
    "conversations_members": (
        "channels:read, groups:read, im:read or mpim:read, depending on what kind of conversation it is"
    ),
    "conversations_info": (
        "channels:read, groups:read, im:read or mpim:read, depending on what kind of conversation it is"
    ),
}


#: A write refused here rather than by Slack.
_WriteRefused = SlackToolRefused


@dataclass(frozen=True)
class _RequesterIdentity:
    """A requester name and icon from Slack."""

    name: str
    icon_url: str


def _marked_text(text: str, note: str) -> str:
    """Add attribution to the plain-text notification fallback."""
    return f"{note}\n{text}" if text else note


def _native_content_error(
    *, markdown_text: str | None, text: str | None, blocks: Any,
    attachments: Any, metadata: Any, parse: str | None,
    mrkdwn: bool | None, link_names: bool | None,
) -> tuple[str, str] | None:
    if markdown_text is not None:
        if not isinstance(markdown_text, str) or not markdown_text.strip():
            return "markdown_text_invalid", "markdown_text must be a nonempty string"
        if text is not None or blocks is not None:
            return "markdown_text_conflict", "markdown_text cannot be combined with text or blocks"
        if len(markdown_text) > 12_000:
            return "markdown_text_too_long", "markdown_text exceeds Slack's 12,000-character limit"
    if attachments is not None and (
        not isinstance(attachments, list)
        or len(attachments) > 100
        or any(
            not isinstance(item, dict)
            or "actions" in item
            or ("blocks" in item and slack_blocks.apply_block_policy(item["blocks"]) is None)
            for item in attachments
        )
    ):
        return "attachments_invalid", "attachments must be at most 100 objects without interactive controls"
    if metadata is not None and metadata != {} and (
        not isinstance(metadata, dict)
        or not isinstance(metadata.get("event_type"), str)
        or not isinstance(metadata.get("event_payload"), dict)
    ):
        return "metadata_invalid", "metadata must contain an event_type string and event_payload object"
    if parse is not None and parse not in ("none", "full"):
        return "parse_invalid", "parse must be none or full"
    if mrkdwn is not None and not isinstance(mrkdwn, bool):
        return "mrkdwn_invalid", "mrkdwn must be a boolean"
    if link_names is not None and not isinstance(link_names, bool):
        return "link_names_invalid", "link_names must be a boolean"
    return None


def _refusal_json(
    code: str,
    detail: str = "",
    *,
    chat_id: str = "",
    message_id: str = "",
) -> str:
    """Report a write refusal without claiming a message's state."""
    return slack_refusal_json(logger, "write", code, detail, chat_id=chat_id, message_id=message_id)


class SlackPostToolkit(SlackMethodToolkitBase):
    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            _post_tool_definition(),
            _edit_tool_definition(),
            _delete_tool_definition(),
        ]

    """The three posting tools, scoped to the Slack request in flight."""

    #: Wall-clock limit for one write call, including retries and membership checks.
    DEFAULT_TIMEOUT_SECONDS = 60.0

    #: Replaced per request by the facts of the install it arrived from.
    _workspace_team = ""
    _workspace_count = 0
    _render_tables = slack_blocks.RENDER_TABLES_DEFAULT
    _bot_name = ""

    def __init__(self, *, bot_name_resolver: Callable[[Any], str] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._resolve_bot_name = bot_name_resolver or (lambda name: str(name or "").strip() or "App")
        # Cache entries are scoped to the verified workspace.
        self._members_cache: dict[tuple[str, str], tuple[float, frozenset[str]]] = {}
        # The workspace URL and bot user ID come from one auth.test answer.
        self._workspace_auth: dict[str, tuple[str, str]] = {}
        # Direct messages already opened, keyed by workspace and user, so that a
        # turn writing to one person twice opens the conversation once.
        self._direct_messages: dict[tuple[str, str], str] = {}
        # User IDs and profile data are scoped to the verified workspace.
        self._identities: dict[tuple[str, str], tuple[float, _RequesterIdentity]] = {}

    # ── request context ──────────────────────────────────────────────────

    async def _load_settings(self, metadata: Mapping[str, Any]) -> Mapping[str, Any]:
        """Load current settings for the bound workspace."""
        slack = await super()._load_settings(metadata)
        self._workspace_team = "" if self._client is not None else self._workspaces.team_of_token(self._bot_token)
        # Read off the answer rather than by a second config read: the
        # selected mapping still carries the ``workspaces`` key it was selected
        # from, and ``slack_workspace_blocks`` is the one reading of it.
        self._workspace_count = len(slack_workspace_blocks(slack))
        self._render_tables = slack_blocks.render_tables_setting(slack)
        self._bot_name = self._resolve_bot_name(slack.get("bot_name"))
        return slack

    def _workspace_key(self) -> str:
        """What the per-workspace caches are keyed by for the request in flight."""
        return self._workspace_team or self._bot_token

    # ── the audience decision and the request's own facts ────────────────

    @staticmethod
    def _enforce_audience(metadata: Mapping[str, Any]) -> bool:
        """Read the settled audience decision or refuse the request."""
        if METADATA_ENFORCE_AUDIENCE_KEY not in metadata:
            raise _WriteRefused(
                "audience_unsettled",
                "this request holds no Slack audience decision, so no side that"
                " has the configuration has said whether anything posted here"
                " is held to the rule",
            )
        return bool(metadata.get(METADATA_ENFORCE_AUDIENCE_KEY))

    @staticmethod
    def _target_must_be_named(metadata: Mapping[str, Any]) -> bool:
        """Require an explicit target when a local reply is unavailable."""
        return bool(metadata.get(METADATA_WRITE_TARGET_REQUIRED_KEY))

    @staticmethod
    def _origin(metadata: Mapping[str, Any]) -> str:
        """Return the trusted source conversation or refuse the request."""
        origin = str(metadata.get("slack_channel_id") or "").strip()
        if not origin:
            raise _WriteRefused(
                "trusted_slack_channel_context_required",
                "this request carries no Slack conversation, so there is nowhere to post and nothing to fall back to",
            )
        return origin

    def _require_one_installation(self, metadata: Mapping[str, Any]) -> None:
        """Refuse membership comparisons across Slack installations."""
        refusal = cross_installation_refusal(
            str(metadata.get(METADATA_TEAM_KEY) or "").strip(),
            self._workspace_team,
            self._workspace_count,
            code="write_cross_workspace_comparison",
        )
        if refusal is not None:
            raise refusal

    # ── membership, and what widens ──────────────────────────────────────

    async def _conversation_members(self, chat_id: str) -> frozenset[str]:
        """Read and cache one conversation member list."""
        now = float(self._monotonic())
        key = (self._workspace_key(), chat_id)
        cached = self._members_cache.get(key)
        if cached is not None and now - cached[0] < _MEMBERS_CACHE_SECONDS:
            return cached[1]

        try:
            settled = await conversation_members(self._call, chat_id, page_limit=_MEMBERS_PAGE_LIMIT)
        except IncompleteMembers as exc:
            detail = f"Slack did not supply a usable member cursor for {chat_id} ({exc}), so its audience is unknown"
            raise _WriteRefused("conversation_members_incomplete", detail) from exc
        _cache_room(self._members_cache, _MAX_MEMBERS_CACHE_ENTRIES)
        self._members_cache[key] = (now, settled)
        return settled

    async def _conversation_info(self, chat_id: str) -> dict[str, Any]:
        """Return a conversation's type, privacy, and sharing flags."""
        return await conversation_info(self._call, chat_id)

    async def _audience(self, chat_id: str) -> Audience:
        """Resolve who can read messages in the target conversation."""
        if chat_id.startswith(_USER_ID_PREFIXES):
            return Audience(workspace_wide=False, members=frozenset({chat_id}), external=False)
        info = await self._conversation_info(chat_id)
        probe = effective_audience(info)
        if probe.workspace_wide:
            return probe
        return effective_audience(info, await self._conversation_members(chat_id))

    async def _newcomers(self, origin_id: str, target_id: str) -> tuple[str, ...] | None:
        """Return people reached by the target but not the source."""
        source = await self._audience(origin_id)
        target = await self._audience(target_id)
        if write_permitted(source, target):
            return ()
        return audience_newcomers(target, source)

    async def _unnameable_sentence(self, target_id: str) -> str:
        """Describe a target with unknown membership."""
        target = await self._audience(target_id)
        if target.workspace_wide:
            return workspace_question(target_id)
        return external_question(target_id)

    async def widening_question(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None,
    ) -> str | None:
        """Return a confirmation question if posting widens the audience."""
        if tool_name not in WIDENING_TOOL_NAMES:
            return None
        if not isinstance(arguments, Mapping):
            return None
        requested = str(arguments.get("chat_id") or "").strip()
        if not requested:
            return None

        turn_metadata = self._runtime_metadata()
        try:
            enforced = self._enforce_audience(turn_metadata)
            origin_id = self._origin(turn_metadata)
        except _WriteRefused:
            return None
        if not enforced:
            return None
        if requested == origin_id or not _SLACK_ID_RE.match(requested):
            return None
        if not self._requester(turn_metadata):
            # Nobody to ask. The tool refuses this, and refusing is not a
            # question, so nothing is returned here.
            return None

        try:
            await self._load_settings(turn_metadata)
            self._require_one_installation(turn_metadata)
            newcomers = await self._newcomers(origin_id, requested)
        except (_WriteRefused, SlackWorkspaceUnresolved, _SlackCallFailure):
            # The tool reports its own refusal after the confirmation precheck.
            return None
        if newcomers == ():
            # Nobody new, whatever kind of target this is.
            return None
        if newcomers is None:
            try:
                return await self._unnameable_sentence(requested)
            except (SlackWorkspaceUnresolved, _SlackCallFailure):
                return None
        if requested.startswith(_USER_ID_PREFIXES):
            return direct_message_question(f"<@{requested}>")
        return widening_question(requested, newcomers, lambda who: f"<@{who}>")

    async def _resolve_target(
        self,
        metadata: Mapping[str, Any],
        chat_id: Any,
        enforced: bool,
        origin_id: str,
        *,
        may_widen: bool,
    ) -> str:
        """Resolve the target and enforce posting permissions."""
        requested = str(chat_id or "").strip()
        if not requested:
            return origin_id
        if requested == origin_id:
            return origin_id

        names_a_person = requested.startswith(_USER_ID_PREFIXES)
        opened = await self._open_direct_message(requested) if names_a_person else requested
        if opened == origin_id:
            return opened
        if not enforced or not may_widen:
            # ``enforce_audience: false`` holds no bound, and a delete has no
            # audience to widen. Both reach the target with nothing checked.
            return opened

        # Compare audiences only within one verified installation.
        self._require_one_installation(metadata)
        newcomers = await self._newcomers(origin_id, opened)
        if newcomers == ():
            return opened
        if not self._requester(metadata):
            raise _WriteRefused(
                "write_target_widens_audience",
                unattended_write_refusal(opened, newcomers, names_a_person),
            )
        # Somebody to ask, so the rail put whatever question this needed. The
        # tool reaching here means the question was answered yes.
        return opened

    async def _open_direct_message(self, user_id: str) -> str:
        """Find or open a direct message with one person."""
        key = (self._workspace_key(), user_id)
        known = self._direct_messages.get(key)
        if known:
            return known
        answer = await self._call("conversations_open", users=user_id)
        channel = answer.get("channel")
        opened = ""
        if isinstance(channel, Mapping):
            opened = str(channel.get("id") or "").strip()
        if not opened:
            raise _WriteRefused(
                "direct_message_unavailable",
                f"Slack opened no conversation with {user_id} and returned no id, so there is nowhere to post",
            )
        self._direct_messages[key] = opened
        return opened

    # ── attribution ──────────────────────────────────────────────────────

    @staticmethod
    def _identity_fields(answer: Mapping[str, Any]) -> _RequesterIdentity:
        """Extract a requester name and icon from users.info."""
        user = answer.get("user")
        raw = user.get("profile") if isinstance(user, Mapping) else None
        profile = raw if isinstance(raw, Mapping) else {}
        return _RequesterIdentity(
            name=str(profile.get("display_name") or "").strip() or str(profile.get("real_name") or "").strip(),
            icon_url=str(profile.get("image_512") or "").strip(),
        )

    async def _requester_identity(self, user_id: str) -> _RequesterIdentity:
        """Read the requester display name and icon from Slack."""
        key = (self._workspace_key(), user_id)
        now = float(self._monotonic())
        cached = self._identities.get(key)
        if cached is not None and now - cached[0] < _IDENTITY_CACHE_SECONDS:
            return cached[1]
        try:
            answer = await self._call("users_info", user=user_id)
        except _SlackCallFailure as exc:
            logger.warning(
                "slack post: users.info for %s was refused (%s); posting under this app's own name and face",
                user_id,
                exc.code,
            )
            return _RequesterIdentity("", "")
        identity = self._identity_fields(answer)
        _cache_room(self._identities, _MAX_IDENTITY_CACHE_ENTRIES)
        self._identities[key] = (now, identity)
        return identity

    def _attribution_note(self, user_id: str, bot_user_id: str) -> str:
        bot = f"<@{bot_user_id}>" if bot_user_id else self._bot_name
        return f"Posted by {bot} on behalf of <@{user_id}>"

    def _attributed(
        self,
        chunk: str,
        blocks: list[dict[str, Any]] | None,
        note: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Prepend attribution to the message and its fallback text."""
        body = blocks or slack_blocks.section_blocks(chunk)
        if len(body) >= slack_blocks.MAX_BLOCKS_PER_MESSAGE:
            raise _WriteRefused(
                "as_requester_leaves_no_room_to_disclose",
                f"Slack accepts {slack_blocks.MAX_BLOCKS_PER_MESSAGE} blocks in"
                f" one message and this message holds all of them, so the line"
                f" saying who posted it does not fit. That line is not optional"
                f" under as_requester, so send fewer blocks or post without the"
                f" flag",
            )
        attribution = {"type": "context", "elements": [{"type": "mrkdwn", "text": note}]}
        fallback = _marked_text(chunk, note)
        if len(fallback) > MAX_SLACK_POST_FALLBACK_LENGTH:
            raise _WriteRefused(
                "post_text_too_long",
                f"text is {len(fallback)} characters with attribution. Slack accepts at most "
                f"{MAX_SLACK_POST_FALLBACK_LENGTH}. "
                "Shorten text or post without as_requester",
            )
        return fallback, [attribution, *body]

    def _render(self, text: str) -> list[tuple[str, list[dict[str, Any]] | None]]:
        """Render each text chunk within Slack's per-message limits."""
        return [
            (chunk, slack_blocks.render_blocks(chunk, render_tables=self._render_tables))
            for chunk in split_text(normalize_slack_mrkdwn(text))
        ]

    async def _workspace_identity(self) -> tuple[str, str]:
        """Read the workspace URL and bot user ID from the same auth.test call."""
        known = self._workspace_auth.get(self._bot_token)
        if known is None:
            try:
                answer = await self._call("auth_test")
            except _SlackCallFailure:
                return "", ""
            known = (
                str(answer.get("url") or "").strip(),
                str(answer.get("user_id") or "").strip(),
            )
            self._workspace_auth[self._bot_token] = known
        return known

    async def _permalink(self, chat_id: str, message_id: str) -> str:
        """Build a permalink from the cached workspace URL."""
        known, _ = await self._workspace_identity()
        if not known or not message_id:
            return ""
        return f"{known.rstrip('/')}/archives/{quote(chat_id)}/p{quote(message_id.replace('.', ''))}"

    def _slack_refusal(self, exc: _SlackCallFailure, *, chat_id: str, message_id: str = "") -> str:
        """Include the missing scope in a Slack refusal when known."""
        return _refusal_json(
            exc.code,
            slack_failure_detail(
                exc,
                _WRITE_FAILURE_DETAIL,
                scope=_SCOPE_FOR_METHOD.get(exc.method, ""),
            ),
            chat_id=chat_id,
            message_id=message_id,
        )

    @staticmethod
    def _message_id_refusal(message_id: str, chat_id: str) -> str | None:
        """Why *message_id* cannot be acted on, or ``None`` when it can."""
        if problem := slack_message_id_error(message_id):
            return _refusal_json(*problem, chat_id=chat_id, message_id=message_id[:32])
        return None

    @checks_slack_ids
    async def post_message(
        self,
        chat_id: str | None = None,
        text: str | None = None,
        blocks: Any = None,
        reply_to_message_id: str | None = None,
        also_post_to_chat: bool = False,
        visible_only_to_user_id: str | None = None,
        unfurl_links: bool | None = None,
        unfurl_media: bool | None = None,
        as_requester: bool = False,
        markdown_text: str | None = None,
        attachments: Any = None,
        metadata: Any = None,
        parse: str | None = None,
        mrkdwn: bool | None = None,
        link_names: bool | None = None,
        unfurl_app_links: bool | None = None,
    ) -> str:
        """Post one message, and say where it landed."""
        if text is not None and not isinstance(text, str):
            return _refusal_json("text_invalid", "text must be a string")
        if problem := _native_content_error(
            markdown_text=markdown_text, text=text, blocks=blocks,
            attachments=attachments, metadata=metadata, parse=parse,
            mrkdwn=mrkdwn, link_names=link_names,
        ):
            return _refusal_json(*problem)
        if unfurl_app_links is not None and not isinstance(unfurl_app_links, bool):
            return _refusal_json("unfurl_app_links_invalid", "unfurl_app_links must be a boolean")
        if markdown_text is not None and as_requester:
            return _refusal_json(
                "markdown_text_placement_invalid",
                "markdown_text cannot be combined with as_requester because the authorship disclosure uses blocks",
            )
        if visible_only_to_user_id and (metadata is not None or mrkdwn is not None or unfurl_app_links is not None):
            return _refusal_json(
                "ephemeral_option_unsupported",
                "ephemeral posts do not support event metadata, mrkdwn or unfurl_app_links",
            )
        turn_metadata = self._runtime_metadata()
        try:
            enforced = self._enforce_audience(turn_metadata)
            origin_id = self._origin(turn_metadata)
        except _WriteRefused as refused:
            return _refusal_json(refused.code, refused.detail)

        # A thread ID can identify the destination when chat_id is omitted.
        if self._target_must_be_named(turn_metadata) and not (
            str(chat_id or "").strip() or str(reply_to_message_id or "").strip()
        ):
            return _refusal_json(
                "write_target_required",
                "this deployment posts no reply of its own, so every message"
                " must name where it goes. Pass chat_id to name a conversation."
                " Pass reply_to_message_id to name a thread in the conversation"
                " this turn came from. That conversation is a valid destination."
                " This refusal gives that conversation's id in its chat_id"
                " field, so pass that id to post there",
                chat_id=origin_id,
            )

        try:
            await self._load_settings(turn_metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _refusal_json(unresolved.code, unresolved.detail, chat_id=origin_id)

        body = text or ""
        declared = slack_blocks.apply_block_policy(blocks)
        if blocks is not None and declared is None:
            # Refuse a block-only message when the declared blocks are invalid.
            if not body.strip():
                return _refusal_json(
                    "blocks_refused",
                    "the blocks were refused because they are malformed or"
                    " contain interactive controls, and no text was given to"
                    " post in their place",
                    chat_id=origin_id,
                )
            logger.warning("slack post: declared blocks were refused; posting text instead")
        if not body.strip() and declared is None and markdown_text is None and not attachments:
            return _refusal_json(
                "nothing_to_post",
                "neither text nor blocks was given, and an empty message is not something Slack will accept",
                chat_id=origin_id,
            )

        thread_ts = str(reply_to_message_id or "").strip()

        requester = self._requester(turn_metadata)
        ephemeral_to = str(visible_only_to_user_id or "").strip()
        if ephemeral_to:
            if problem := ephemeral_refusal(ephemeral_to, requester):
                return _refusal_json(*problem, chat_id=origin_id)
            if as_requester:
                # An ephemeral message cannot also impersonate its recipient.
                return _refusal_json(
                    "as_requester_cannot_be_ephemeral",
                    "visible_only_to_user_id shows the message to the person who"
                    " asked and to nobody else, and as_requester puts their name"
                    " on it, so together they would show somebody their own name"
                    " and tell them what they already know. Drop one of the two",
                    chat_id=origin_id,
                )

        if as_requester and not requester:
            # Scheduled runs have no requester to attribute.
            return _refusal_json(
                "as_requester_requires_a_requester",
                "as_requester puts the asker's name and face on the message,"
                " and this turn was not started by anybody -- a scheduled run"
                " has no requester and no errand to run. Post without the flag,"
                " under this app's own name",
                chat_id=origin_id,
            )

        # Prepare every part before target resolution, which may open a DM.
        if declared is not None and len(body) > MAX_SLACK_POST_FALLBACK_LENGTH:
            return _refusal_json(
                "post_text_too_long",
                f"text is {len(body)} characters. Slack accepts at most "
                f"{MAX_SLACK_POST_FALLBACK_LENGTH} in one post with blocks. "
                "Shorten text or omit blocks to allow multipart posting",
                chat_id=origin_id,
            )
        pieces = [(body, declared)] if declared is not None or markdown_text is not None else self._render(body)
        if attachments is not None and declared is None and markdown_text is None and not body.strip():
            pieces = [("", None)]
        if len(pieces) != 1 and (attachments is not None or metadata is not None):
            return _refusal_json(
                "native_options_require_one_part",
                "attachments and metadata need one message; shorten text or post parts separately",
                chat_id=origin_id,
            )

        identity = _RequesterIdentity("", "")
        if as_requester:
            identity = await self._requester_identity(requester)
            _, bot_user_id = await self._workspace_identity()
            bot_user_id = (
                bot_user_id if bot_user_id.startswith(_USER_ID_PREFIXES) and _SLACK_ID_RE.fullmatch(bot_user_id) else ""
            )
            note = self._attribution_note(requester, bot_user_id)
            try:
                pieces = [self._attributed(chunk, chunk_blocks, note) for chunk, chunk_blocks in pieces]
            except _WriteRefused as refused:
                return _refusal_json(refused.code, refused.detail, chat_id=origin_id)

        try:
            target_id = await self._resolve_target(turn_metadata, chat_id, enforced, origin_id, may_widen=True)
        except _WriteRefused as refused:
            return _refusal_json(refused.code, refused.detail, chat_id=origin_id)
        except _SlackCallFailure as exc:
            return self._slack_refusal(exc, chat_id=origin_id)

        if ephemeral_to and target_id != origin_id:
            return _refusal_json(*EPHEMERAL_LEAVES_THIS_CONVERSATION, chat_id=origin_id)

        if as_requester and target_id == origin_id:
            # Reject as_requester in its source conversation. It changes the post identity.
            return _refusal_json(
                "as_requester_needs_another_conversation",
                "as_requester puts the asker's name on a message they asked to"
                " have delivered somewhere else, and this message goes to the"
                " conversation the request came from, where their own message is"
                " already visible. Pass chat_id to name where it should go, or"
                " post without the flag",
                chat_id=origin_id,
            )

        posted: list[str] = []
        first_thread = thread_ts
        for index, (chunk, chunk_blocks) in enumerate(pieces):
            kwargs: dict[str, Any] = {"channel": target_id}
            if markdown_text is not None:
                kwargs["markdown_text"] = markdown_text
            else:
                kwargs["text"] = chunk
            if chunk_blocks:
                kwargs["blocks"] = chunk_blocks
            for key, value in (
                ("attachments", attachments), ("metadata", metadata), ("parse", parse),
                ("mrkdwn", mrkdwn), ("link_names", link_names),
                ("unfurl_app_links", unfurl_app_links),
            ):
                if value is not None:
                    kwargs[key] = value
            if ephemeral_to:
                kwargs["user"] = ephemeral_to
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
                if also_post_to_chat and index == 0:
                    # Broadcast only the first piece of a long reply.
                    kwargs["reply_broadcast"] = True
            if unfurl_links is not None:
                kwargs["unfurl_links"] = bool(unfurl_links)
            if unfurl_media is not None:
                kwargs["unfurl_media"] = bool(unfurl_media)
            if identity.name:
                # The name is what attributes the message, so the picture is
                # sent only beside it: the asker's face over this app's name
                # would name nobody and claim somebody.
                kwargs["username"] = identity.name
                if identity.icon_url:
                    kwargs["icon_url"] = identity.icon_url

            method = "chat_postEphemeral" if ephemeral_to else "chat_postMessage"
            try:
                answer = await self._call(method, **kwargs)
                message_id = answer.get("ts") or answer.get("message_ts")
                if not isinstance(message_id, str) or slack_message_id_error(message_id):
                    raise _SlackCallFailure("invalid_response", method)
            except _SlackCallFailure as exc:
                if posted:
                    result = json.loads(_refusal_json(
                        exc.code,
                        f"{len(posted)} of {len(pieces)} part(s) were confirmed in {target_id}"
                        f" before {exc.where} failed. More may be visible."
                        f" Do not post the whole message again",
                        chat_id=target_id,
                        message_id=posted[0],
                    ))
                    result.update(parts=len(posted), part_message_ids=posted, thread_id=first_thread)
                    return json.dumps(result, ensure_ascii=False)
                if as_requester and exc.code == "missing_scope":
                    # The identity override requires chat:write.customize.
                    return _refusal_json(
                        exc.code,
                        f"as_requester needs {_CUSTOMIZE_SCOPE} and this"
                        f" installation holds only chat:write, so nothing was"
                        f" posted. Post without the flag, which needs no further"
                        f" grant, or ask the operator to reinstall this app with"
                        f" {_CUSTOMIZE_SCOPE}",
                        chat_id=target_id,
                    )
                return self._slack_refusal(exc, chat_id=target_id)
            posted.append(message_id)
            if not thread_ts and index == 0 and len(pieces) > 1:
                # Thread later pieces under the first message.
                thread_ts = posted[0]

        result: dict[str, Any] = {
            "ok": True,
            "chat_id": target_id,
            "message_id": posted[0],
            "parts": len(posted),
            "thread_id": first_thread,
        }
        if ephemeral_to:
            result["visible_only_to_user_id"] = ephemeral_to
            # Ephemeral messages have no archive URL.
            result["ephemeral"] = True
        else:
            permalink = await self._permalink(target_id, posted[0])
            if permalink:
                result["permalink"] = permalink
        if len(posted) > 1:
            result["part_message_ids"] = posted
        if as_requester:
            # Return the identity used to post, which the caller cannot observe here.
            result["as_requester"] = {
                "user_id": requester,
                "name": identity.name,
                "icon": "the asker's" if identity.icon_url else "this app's",
            }
        logger.info(
            "slack post: %d part(s) into %s%s%s",
            len(posted),
            target_id,
            " (ephemeral)" if ephemeral_to else "",
            f" as {requester}" if as_requester else "",
        )
        return json.dumps(result, ensure_ascii=False)

    @checks_slack_ids
    async def edit_message(
        self,
        message_id: str,
        chat_id: str | None = None,
        text: str | None = None,
        blocks: Any = None,
        also_post_to_chat: bool = False,
        markdown_text: str | None = None,
        attachments: Any = None,
        metadata: Any = None,
        parse: str | None = None,
        link_names: bool | None = None,
    ) -> str:
        """Rewrite one message this app posted."""
        if text is not None and not isinstance(text, str):
            return _refusal_json("text_invalid", "text must be a string", message_id=message_id)
        if problem := _native_content_error(
            markdown_text=markdown_text, text=text, blocks=blocks,
            attachments=attachments, metadata=metadata, parse=parse,
            mrkdwn=None, link_names=link_names,
        ):
            return _refusal_json(*problem, message_id=message_id)
        turn_metadata = self._runtime_metadata()
        try:
            enforced = self._enforce_audience(turn_metadata)
            origin_id = self._origin(turn_metadata)
        except _WriteRefused as refused:
            return _refusal_json(refused.code, refused.detail)

        raw_id = str(message_id or "").strip()
        if refusal := self._message_id_refusal(raw_id, origin_id):
            return refusal

        try:
            await self._load_settings(turn_metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _refusal_json(unresolved.code, unresolved.detail, chat_id=origin_id)

        body = text or ""
        declared = slack_blocks.apply_block_policy(blocks)
        if blocks is not None and declared is None and not body.strip():
            return _refusal_json(
                "blocks_refused",
                "the blocks were refused because they are malformed or contain"
                " interactive controls, and no text was given to replace them",
                chat_id=origin_id,
                message_id=raw_id,
            )
        if not body.strip() and declared is None and markdown_text is None and attachments is None:
            return _refusal_json(
                "nothing_to_post",
                "neither text nor blocks was given, so there is nothing to"
                " rewrite the message to. To take a message down, use"
                " delete_message",
                chat_id=origin_id,
                message_id=raw_id,
            )

        if markdown_text is not None:
            chunk, chunk_blocks = "", None
        elif declared is not None:
            if len(body) > MAX_SLACK_UPDATE_TEXT_LENGTH:
                return _refusal_json(
                    "edit_text_too_long",
                    f"text is {len(body)} characters. Slack accepts at most "
                    f"{MAX_SLACK_UPDATE_TEXT_LENGTH} for an edit with blocks. Shorten text",
                    chat_id=origin_id,
                    message_id=raw_id,
                )
            chunk, chunk_blocks = body, declared
        else:
            # An edit must fit in one Slack message.
            pieces = self._render(body)
            chunk, chunk_blocks = pieces[0]
            if len(pieces) > 1 or len(chunk) > MAX_SLACK_UPDATE_TEXT_LENGTH:
                return _refusal_json(
                    "edit_text_too_long",
                    f"Slack accepts at most {MAX_SLACK_UPDATE_TEXT_LENGTH}"
                    f" characters when a message is edited, a tenth of what it"
                    f" accepts when one is posted, and this is longer. Shorten"
                    f" the replacement, or post the rest as a new message",
                    chat_id=origin_id,
                    message_id=raw_id,
                )

        try:
            target_id = await self._resolve_target(turn_metadata, chat_id, enforced, origin_id, may_widen=True)
        except _WriteRefused as refused:
            return _refusal_json(refused.code, refused.detail, chat_id=origin_id, message_id=raw_id)
        except _SlackCallFailure as exc:
            return self._slack_refusal(exc, chat_id=origin_id, message_id=raw_id)

        kwargs: dict[str, Any] = {"channel": target_id, "ts": raw_id}
        if markdown_text is not None:
            kwargs["markdown_text"] = markdown_text
        elif text is not None or chunk:
            kwargs["text"] = chunk
        if chunk_blocks:
            kwargs["blocks"] = chunk_blocks
        elif blocks is not None:
            # An empty list removes existing blocks; omission preserves them.
            kwargs["blocks"] = []
        if also_post_to_chat:
            kwargs["reply_broadcast"] = True
        for key, value in (
            ("attachments", attachments), ("metadata", metadata),
            ("parse", parse), ("link_names", link_names),
        ):
            if value is not None:
                kwargs[key] = value
        try:
            answer = await self._call("chat_update", **kwargs)
        except _SlackCallFailure as exc:
            return self._slack_refusal(exc, chat_id=target_id, message_id=raw_id)

        settled = str(answer.get("ts") or raw_id).strip()
        result: dict[str, Any] = {
            "ok": True,
            "chat_id": target_id,
            "message_id": settled,
        }
        permalink = await self._permalink(target_id, settled)
        if permalink:
            result["permalink"] = permalink
        logger.info("slack edit: %s in %s", settled, target_id)
        return json.dumps(result, ensure_ascii=False)

    @checks_slack_ids
    async def delete_message(
        self,
        message_id: str,
        chat_id: str | None = None,
    ) -> str:
        """Take one message this app posted out of the conversation."""
        metadata = self._runtime_metadata()
        try:
            enforced = self._enforce_audience(metadata)
            origin_id = self._origin(metadata)
        except _WriteRefused as refused:
            return _refusal_json(refused.code, refused.detail)

        raw_id = str(message_id or "").strip()
        if refusal := self._message_id_refusal(raw_id, origin_id):
            return refusal

        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _refusal_json(unresolved.code, unresolved.detail, chat_id=origin_id)
        try:
            # Deletion does not widen who can read the message.
            target_id = await self._resolve_target(metadata, chat_id, enforced, origin_id, may_widen=False)
        except _WriteRefused as refused:
            return _refusal_json(refused.code, refused.detail, chat_id=origin_id, message_id=raw_id)
        except _SlackCallFailure as exc:
            return self._slack_refusal(exc, chat_id=origin_id, message_id=raw_id)

        try:
            await self._call("chat_delete", channel=target_id, ts=raw_id)
        except _SlackCallFailure as exc:
            return self._slack_refusal(exc, chat_id=target_id, message_id=raw_id)

        logger.info("slack delete: %s in %s", raw_id, target_id)
        return json.dumps(
            {"ok": True, "chat_id": target_id, "message_id": raw_id, "deleted": True},
            ensure_ascii=False,
        )

    # ── registration ─────────────────────────────────────────────────────

_CHAT_ID_PARAM = {
    "type": "string",
    "description": (
        "Slack conversation ID, such as C0123456789 for a channel or "
        "U0123456789 for a person. Omit it to use this conversation. "
        "Get the ID from find_by_name or an earlier result. Never guess it."
    ),
}


_POST_CHAT_ID_PARAM = {
    "type": "string",
    "description": (
        "Slack conversation ID, such as C0123456789 for a channel or "
        "U0123456789 for a person. Get the ID from find_by_name or an earlier "
        "result. Never guess it. Omit it for this conversation. Some "
        "deployments refuse a post that names neither chat_id nor "
        "reply_to_message_id. That refusal reports this conversation's ID for "
        "you to pass back."
    ),
}


_ELSEWHERE = (
    "\nPass chat_id for another conversation. A conversation whose readers can "
    "already read this one needs no approval. If it reaches new readers, this "
    "tool asks the requester to approve before it sends, so do not ask them "
    "yourself. A public channel reaches the whole workspace, so a "
    "message from a nonpublic conversation needs approval. A scheduled run "
    "cannot ask and is refused. A person ID opens their direct message."
)


def _post_tool_definition() -> dict[str, Any]:
    """The card for ``post_message``."""
    properties: dict[str, Any] = {
        "text": {
            "type": "string",
            "description": (
                "Message text. Supply nonempty text unless valid blocks are supplied. "
                "Rendered like an ordinary reply. Long text splits "
                "into threaded parts. With blocks, use text as a brief fallback "
                "that makes sense on its own. Belle refuses a fallback above 40,000 characters before posting."
            ),
        },
        "blocks": {
            "type": "array",
            "items": {"type": "object"},
            "description": (
                "Slack Block Kit uses the same validation as a blockkit fence. "
                "May be supplied without text or together with text. "
                "Interactive controls fail validation. If blocks fail, the tool "
                "posts text or refuses an empty fallback."
            ),
        },
        "markdown_text": {
            "type": "string",
            "description": "Native Slack markdown, at most 12,000 characters. Cannot be combined with text, blocks or as_requester.",
        },
        "attachments": {
            "type": "array", "items": {"type": "object"},
            "description": "Slack message attachments without interactive actions. Sent on one message only.",
        },
        "metadata": {
            "type": "object",
            "description": "Slack event_type and event_payload. Visible to workspace members; sent on one message only.",
        },
        "parse": {"type": "string", "enum": ["none", "full"], "description": "Slack text parsing mode."},
        "mrkdwn": {"type": "boolean", "description": "Enable Slack mrkdwn parsing of top-level text."},
        "link_names": {"type": "boolean", "description": "Let Slack link named user groups in top-level text."},
        "unfurl_app_links": {"type": "boolean", "description": "Unfurl links from installed apps."},
        "reply_to_message_id": {
            "type": "string",
            "description": (
                "Message ID to reply to in its thread. Copy it from an earlier result. "
                "This selects the destination only; also supply text or valid blocks."
            ),
        },
        "also_post_to_chat": {
            "type": "boolean",
            "default": False,
            "description": (
                "With reply_to_message_id, also show the reply to the whole "
                "conversation. Use this only when readers outside the thread need it."
            ),
        },
        "visible_only_to_user_id": {
            "type": "string",
            "description": (
                "Show an ephemeral message only to this turn's requester. "
                "It cannot be edited or deleted and does not enter history."
            ),
        },
        "unfurl_links": {
            "type": "boolean",
            "description": ("Expand links into previews. Omit to use Slack's default."),
        },
        "unfurl_media": {
            "type": "boolean",
            "description": ("Expand media links into previews. Omit to use Slack's default."),
        },
        "as_requester": {
            "type": "boolean",
            "default": False,
            "description": (
                "Post under the requester's name and face when they directed a "
                "message to another conversation. The message says this app posted "
                "it. This fails without a requester or another chat_id."
            ),
        },
    }
    properties = {"chat_id": _POST_CHAT_ID_PARAM, **properties}

    return {
        "name": POST_MESSAGE,
        "description": (
            "Post an additional Slack message here or in another conversation. "
            "Every call must include nonempty text or valid blocks; both are allowed. "
            "reply_to_message_id alone posts nothing. "
            "Use a normal reply for the ordinary answer to avoid duplicates."
            + _ELSEWHERE
            + "\nWith blocks, text is the preview and fallback. "
            "Posts use this app's identity unless you set as_requester. "
            "That flag uses only this turn's requester, discloses Belle on every part, and needs another chat_id. "
            "The result includes chat_id, message_id and thread_id. Split posts list their part_message_ids. "
            "If a later part fails, the error lists the parts already sent; do not retry the whole post. "
            "A persistent post may include a permalink."
        ),
        "input_params": {"type": "object", "properties": properties, "required": []},
    }


def _edit_tool_definition() -> dict[str, Any]:
    """The card for ``edit_message``."""
    properties: dict[str, Any] = {
        "message_id": {
            "type": "string",
            "description": (
                "Slack ID of the message to replace. Copy it from an earlier result for the same conversation."
            ),
        },
        "text": {
            "type": "string",
            "description": (
                "Complete replacement text. Edits allow at most 4,000 characters. "
                "With blocks, this is the notification preview and fallback."
            ),
        },
        "blocks": {
            "type": "array",
            "items": {"type": "object"},
            "description": ("Use validated Block Kit. An empty array clears existing blocks."),
        },
        "markdown_text": {
            "type": "string",
            "description": "Native Slack markdown, at most 12,000 characters. Cannot be combined with text or blocks.",
        },
        "attachments": {
            "type": "array", "items": {"type": "object"},
            "description": "Replacement Slack attachments. An empty array removes existing attachments.",
        },
        "metadata": {
            "type": "object",
            "description": "Slack event_type and event_payload; an empty object removes existing metadata.",
        },
        "parse": {"type": "string", "enum": ["none", "full"], "description": "Slack text parsing mode."},
        "link_names": {"type": "boolean", "description": "Let Slack link named user groups in top-level text."},
        "also_post_to_chat": {
            "type": "boolean",
            "default": False,
            "description": ("Also show a thread reply in the conversation."),
        },
    }
    properties = {"chat_id": _CHAT_ID_PARAM, **properties}

    return {
        "name": EDIT_MESSAGE,
        "description": (
            "Replace a message this app posted. Prefer editing messages this "
            "tool posted. The connector manages its own turn cards and replies. "
            "Slack refuses edits to other authors' or ephemeral messages."
            + _ELSEWHERE
            + "\nSend the complete replacement. The result includes chat_id and "
            "message_id. It may include a permalink."
        ),
        "input_params": {
            "type": "object",
            "properties": properties,
            "required": ["message_id"],
        },
    }


def _delete_tool_definition() -> dict[str, Any]:
    """The card for ``delete_message``."""
    properties: dict[str, Any] = {
        "message_id": {
            "type": "string",
            "description": ("Slack ID of the message to delete. Copy it from an earlier result."),
        },
    }
    properties = {"chat_id": _CHAT_ID_PARAM, **properties}

    return {
        "name": DELETE_MESSAGE,
        "description": (
            "Permanently delete a message this app posted, without confirmation. "
            "Prefer editing if the message can still be useful. The connector "
            "manages its own turn cards and replies. Slack refuses deletion of "
            "other authors' messages. Pass chat_id for another conversation. "
            "The result includes chat_id and message_id."
        ),
        "input_params": {
            "type": "object",
            "properties": properties,
            "required": ["message_id"],
        },
    }
