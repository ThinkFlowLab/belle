"""Read authorized bookmarks and change bookmarks in the request conversation."""

import json
import logging
from collections.abc import Mapping
from typing import Any

from .history import SlackHistoryToolkit, _HistoryRefused
from .history_policy import (
    SlackWorkspaceUnresolved,
)
from .json_toolkit import (
    SlackMethodToolkitBase,
    slack_echo,
    slack_failure_detail,
    slack_refusal_json,
)
from .reactions import _emoji_name
from .tool_common import (
    _iso_utc,
    _positive_timestamp,
    _SlackCallFailure,
)
from .tool_context import (
    SlackToolRefused,
    checks_slack_ids,
    slack_tool_error,
)

logger = logging.getLogger(__name__)

SLACK_CHANNEL_ID_KEY = "slack_channel_id"

_BOOKMARK_ADD = "bookmarks_add"
_BOOKMARK_EDIT = "bookmarks_edit"
_BOOKMARK_REMOVE = "bookmarks_remove"
_BOOKMARK_LIST = "bookmarks_list"

_BOOKMARK_WRITE_SCOPE = "bookmarks:write"
_BOOKMARK_READ_SCOPE = "bookmarks:read"

_ACTION_ADD = "add"
_ACTION_EDIT = "edit"
_ACTION_REMOVE = "remove"
_ACTIONS = (_ACTION_ADD, _ACTION_EDIT, _ACTION_REMOVE)

_ACTION_ARGUMENTS: dict[str, frozenset[str]] = {
    _ACTION_ADD: frozenset(
        {
            "title",
            "url",
            "emoji_name",
            "bookmark_type",
            "entity_id",
            "access_level",
            "parent_bookmark_id",
        }
    ),
    _ACTION_EDIT: frozenset({"bookmark_id", "title", "url", "emoji_name"}),
    _ACTION_REMOVE: frozenset({"bookmark_id", "quip_section_id"}),
}

_OUR_NAME_FOR = {"title": "title", "link": "url", "emoji": "emoji_name"}

_BOOKMARK_FAILURE_DETAIL = {
    "too_many_bookmarks": (
        "this conversation holds as many bookmarks as Slack allows, so nothing"
        " further can be added until somebody removes one; calling again will"
        " not help"
    ),
    "bookmark_not_found": (
        "this conversation holds no bookmark with that id; a bookmark id is not"
        " transferable between conversations, so one taken from elsewhere names"
        " nothing here"
    ),
    "invalid_bookmark_type": (
        "Slack will not create a bookmark of that type through the API. A link"
        " bookmark, which is the default, is what it accepts; a second call"
        " with the same type will not change that"
    ),
    "invalid_arguments": (
        "Slack refused the arguments as a set. A bookmark with no url and a"
        " folder are both refused this way: a folder is made by a person in"
        " Slack and cannot be created through the API, although a bookmark can"
        " be placed inside one that already exists"
    ),
    "channel_not_found": ("Slack does not recognise this conversation, or the bot is not in it"),
}


#: A bookmark write refused here rather than by Slack.
_BookmarkRefused = SlackToolRefused


def _clean(value: Any) -> str | None:
    """Trim an argument while preserving empty strings that clear a field."""
    if value is None:
        return None
    return str(value).strip()


def _write_refusal_json(
    code: str,
    detail: str = "",
    *,
    chat_id: str = "",
    action: str = "",
    bookmark_id: str = "",
) -> str:
    """Report a write refusal without asserting the bookmark bar's state."""
    return slack_refusal_json(
        logger,
        "bookmark write",
        code,
        detail,
        chat_id=chat_id,
        action=action,
        bookmark_id=bookmark_id,
    )


def _read_refusal_json(code: str, detail: str = "", *, chat_id: str = "") -> str:
    """Return a failed listing with an empty bookmarks array."""
    logger.warning("slack bookmarks read refused: %s%s", code, f" -- {detail}" if detail else "")
    fields = {"chat_id": chat_id} if chat_id else {}
    return slack_tool_error(code, detail, **fields, bookmarks=[])


def _scope_for(method: str) -> str:
    """Return the scope for the refused API method."""
    return _BOOKMARK_READ_SCOPE if method == _BOOKMARK_LIST else _BOOKMARK_WRITE_SCOPE


def _normalize_bookmark(item: Mapping[str, Any]) -> dict[str, Any]:
    """Map Slack fields to tool fields and omit empty values."""
    record: dict[str, Any] = {
        "bookmark_id": str(item.get("id") or "").strip(),
        "title": str(item.get("title") or ""),
        "url": str(item.get("link") or ""),
        "emoji_name": str(item.get("emoji") or "").strip().strip(":"),
        "bookmark_type": str(item.get("type") or "").strip(),
        "rank": str(item.get("rank") or ""),
    }
    optional = {
        "icon_url": str(item.get("icon_url") or "").strip(),
        "entity_id": str(item.get("entity_id") or "").strip(),
        "access_level": str(item.get("access_level") or "").strip(),
        "parent_bookmark_id": str(item.get("parent_id") or "").strip(),
        "shortcut_id": str(item.get("shortcut_id") or "").strip(),
        "app_id": str(item.get("app_id") or "").strip(),
        "updated_by_user_id": str(item.get("last_updated_by_user_id") or "").strip(),
    }
    record.update({key: value for key, value in optional.items() if value})
    for key, source in (
        ("created_iso_utc", "date_created"),
        ("updated_iso_utc", "date_updated"),
    ):
        stamped = _iso_utc(_positive_timestamp(item.get(source)))
        if stamped:
            record[key] = stamped
    return {key: value for key, value in record.items() if value != ""}


class SlackBookmarkToolkit(SlackMethodToolkitBase):
    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            self._write_tool_definition(),
            self._read_tool_definition(),
        ]

    @staticmethod
    def _write_tool_definition() -> dict[str, Any]:
        """Describe bookmark writes in this conversation."""
        return {
            "name": "write_bookmark",
            "description": (
                "Add, edit, or remove a shared bookmark in this conversation. "
                "Read existing bookmarks before adding: two identical calls "
                "make two bookmarks. Add requires a title and returns the new "
                "bookmark_id. Edit requires a bookmark_id and a changed title, "
                "url, or emoji_name. Omitted fields stay unchanged. Remove "
                "takes a bookmark_id or Quip quip_section_id. Copy identifiers "
                "from Slack results. A folder must already exist in Slack. "
                "A full bookmark bar requires a person to remove an entry."
            ),
            "input_params": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": list(_ACTIONS),
                        "description": ("What to do: add a bookmark, edit one, or remove one."),
                    },
                    "bookmark_id": {
                        "type": "string",
                        "description": (
                            "Slack bookmark id to edit or remove. Copy it from a Slack result. Add creates the id."
                        ),
                    },
                    "quip_section_id": {
                        "type": "string",
                        "description": ("Quip section id to remove when no bookmark_id is available."),
                    },
                    "title": {
                        "type": "string",
                        "description": ("Visible title. Required for add and optional for edit."),
                    },
                    "url": {
                        "type": "string",
                        "description": ("Target URL. Include its query string and fragment."),
                    },
                    "emoji_name": {
                        "type": "string",
                        "description": ("Emoji shortcode without colons, or an emoji character."),
                    },
                    "bookmark_type": {
                        "type": "string",
                        # A schema default would also apply to edit and remove.
                        "description": ("Bookmark type for add only. Omit for a link."),
                    },
                    "entity_id": {
                        "type": "string",
                        "description": ("Target id for a non-link bookmark."),
                    },
                    "access_level": {
                        "type": "string",
                        "enum": ["read", "write"],
                        "description": ("Access granted by a non-link bookmark."),
                    },
                    "parent_bookmark_id": {
                        "type": "string",
                        "description": ("Id of an existing Slack folder for the new bookmark."),
                    },
                },
                "required": ["action"],
            },
        }

    @staticmethod
    def _read_tool_definition() -> dict[str, Any]:
        """Describe the complete bookmark listing."""
        return {
            "name": "read_bookmarks",
            "description": (
                "List every bookmark in this conversation, or pass chat_id for another conversation "
                "whose readers may see the source conversation. The result includes "
                "every bookmark without paging. Each record has its id, title, URL, "
                "and any available emoji, rank, timestamps, and updater. "
                "The result omits empty fields. Compare rank strings lexicographically. "
                "The source audience rule also applies to another chat_id."
            ),
            "input_params": {"type": "object", "properties": {
                "chat_id": {"type": "string", "description": "Another Slack conversation whose bookmarks to read. Omit for this conversation."},
            }, "required": []},
        }

    """Toolkit scoped to the Slack conversation in the active request metadata."""

    #: One deadline for the whole write, which for an edit is a read and a write.
    DEFAULT_TIMEOUT_SECONDS = 30.0
    _deadline_per_call = False

    @staticmethod
    def _chat_id(metadata: Mapping[str, Any]) -> str:
        return str(metadata.get(SLACK_CHANNEL_ID_KEY) or "").strip()

    @staticmethod
    def _timeout_code(method: str) -> str:
        """Named for what ran out, and on which half of the work."""
        if method == _BOOKMARK_LIST:
            return "bookmarks_read_timed_out"
        return "bookmark_write_timed_out"

    @staticmethod
    def _failure_detail(exc: _SlackCallFailure) -> str:
        return slack_failure_detail(exc, _BOOKMARK_FAILURE_DETAIL, scope=_scope_for(exc.method))

    async def write_bookmark(
        self,
        action: str,
        bookmark_id: str | None = None,
        quip_section_id: str | None = None,
        title: str | None = None,
        url: str | None = None,
        emoji_name: str | None = None,
        bookmark_type: str | None = None,
        entity_id: str | None = None,
        access_level: str | None = None,
        parent_bookmark_id: str | None = None,
    ) -> str:
        """Add, edit, or remove a bookmark in this conversation."""
        metadata = self._runtime_metadata()
        chat_id = self._chat_id(metadata)
        chosen = str(action or "").strip().lower()
        supplied = {
            "bookmark_id": _clean(bookmark_id),
            "quip_section_id": _clean(quip_section_id),
            "title": _clean(title),
            "url": _clean(url),
            "emoji_name": _clean(emoji_name),
            "bookmark_type": _clean(bookmark_type),
            "entity_id": _clean(entity_id),
            "access_level": _clean(access_level),
            "parent_bookmark_id": _clean(parent_bookmark_id),
        }

        if not chat_id:
            return _write_refusal_json(
                "trusted_slack_channel_context_required",
                "this request carries no Slack conversation, so there is no"
                " bookmark bar to write to and nothing to fall back to",
            )
        try:
            self._check_action(chosen, supplied)
            method, kwargs = await self._write_arguments(chat_id, chosen, supplied, metadata)
            response = await self._call(method, **kwargs)
        except _BookmarkRefused as refusal:
            return _write_refusal_json(
                refusal.code,
                refusal.detail,
                chat_id=chat_id,
                action=chosen if chosen in _ACTIONS else "",
                bookmark_id=supplied["bookmark_id"] or "",
            )
        except _SlackCallFailure as exc:
            return _write_refusal_json(
                exc.code,
                self._failure_detail(exc),
                chat_id=chat_id,
                action=chosen,
                bookmark_id=supplied["bookmark_id"] or "",
            )

        return self._write_result(chat_id, chosen, supplied, response)

    @staticmethod
    def _check_action(action: str, supplied: Mapping[str, str | None]) -> None:
        """Validate the action and its arguments before calling Slack."""
        if not action:
            raise _BookmarkRefused(
                "action_required",
                "action is required. Pass add to put a bookmark on this"
                " conversation, edit to change one, or remove to take one off.",
            )
        if action not in _ACTIONS:
            raise _BookmarkRefused(
                "action_unknown",
                f"action was {slack_echo(action)!r}, which is not one of {', '.join(_ACTIONS)}.",
            )

        allowed = _ACTION_ARGUMENTS[action]
        unusable = sorted(name for name, value in supplied.items() if value is not None and name not in allowed)
        if unusable:
            article = "a" if action == _ACTION_REMOVE else "an"
            raise _BookmarkRefused(
                "argument_not_valid_for_action",
                f"{', '.join(unusable)} cannot be applied by"
                f" {article} {action}, so passing it would ask for a"
                f" change that would not be made."
                f" {article.capitalize()} {action} takes"
                f" {', '.join(sorted(allowed))}.",
            )

        if action == _ACTION_ADD and not supplied["title"]:
            raise _BookmarkRefused(
                "title_required",
                "title is required when adding a bookmark; it is the text"
                " everybody in the conversation sees on the bar.",
            )
        if action == _ACTION_EDIT and not supplied["bookmark_id"]:
            raise _BookmarkRefused(
                "bookmark_id_required",
                "bookmark_id is required when editing; take it unchanged from a result that reported one.",
            )
        if action == _ACTION_REMOVE and not (supplied["bookmark_id"] or supplied["quip_section_id"]):
            raise _BookmarkRefused(
                "bookmark_id_required",
                "removing needs the bookmark named: pass bookmark_id, or quip_section_id for a Quip section bookmark.",
            )
        if action == _ACTION_EDIT and not any(supplied[name] is not None for name in ("title", "url", "emoji_name")):
            raise _BookmarkRefused(
                "nothing_to_edit",
                "an edit changes the title, the url or the emoji_name, and none"
                " of the three was given, so there is no change to make.",
            )

    @staticmethod
    def _slack_emoji(emoji_name: str) -> str:
        """Normalize a shortcode or emoji for Slack, preserving empty values."""
        if not emoji_name:
            return ""
        return f":{_emoji_name(emoji_name)}:"

    async def _write_arguments(
        self,
        chat_id: str,
        action: str,
        supplied: Mapping[str, str | None],
        metadata: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """Settle the method and arguments one write call is made with."""
        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            raise _BookmarkRefused(unresolved.code, unresolved.detail) from None

        emoji = self._slack_emoji(supplied["emoji_name"] or "")

        if action == _ACTION_REMOVE:
            kwargs: dict[str, Any] = {"channel_id": chat_id}
            if supplied["bookmark_id"]:
                kwargs["bookmark_id"] = supplied["bookmark_id"]
            if supplied["quip_section_id"]:
                kwargs["quip_section_id"] = supplied["quip_section_id"]
            return _BOOKMARK_REMOVE, kwargs

        if action == _ACTION_ADD:
            kwargs = {
                "channel_id": chat_id,
                "title": supplied["title"],
                "type": supplied["bookmark_type"] or "link",
            }
            for name, slack_name in (
                ("url", "link"),
                ("entity_id", "entity_id"),
                ("access_level", "access_level"),
                ("parent_bookmark_id", "parent_id"),
            ):
                if supplied[name] is not None:
                    kwargs[slack_name] = supplied[name]
            if supplied["emoji_name"] is not None:
                kwargs["emoji"] = emoji
            return _BOOKMARK_ADD, kwargs

        # Slack's behavior for omitted edit fields is undocumented.
        current = await self._read_bookmark(chat_id, supplied["bookmark_id"])
        merged = {
            "title": supplied["title"] if supplied["title"] is not None else str(current.get("title") or ""),
            "link": supplied["url"] if supplied["url"] is not None else str(current.get("link") or ""),
            "emoji": emoji if supplied["emoji_name"] is not None else str(current.get("emoji") or "").strip(),
        }
        kwargs = {"channel_id": chat_id, "bookmark_id": supplied["bookmark_id"]}
        for slack_name, value in merged.items():
            # Explicit empty values clear fields; inherited empties are omitted.
            if value or supplied[_OUR_NAME_FOR[slack_name]] is not None:
                kwargs[slack_name] = value
        return _BOOKMARK_EDIT, kwargs

    async def _read_bookmark(self, chat_id: str, bookmark_id: str) -> dict[str, Any]:
        """Find the bookmark in Slack's complete, unpaginated listing."""
        listing = await self._call(_BOOKMARK_LIST, channel_id=chat_id)
        for item in listing.get("bookmarks") or []:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("id") or "").strip() == bookmark_id:
                return dict(item)
        raise _BookmarkRefused(
            "bookmark_not_found",
            f"this conversation holds no bookmark with the id"
            f" {slack_echo(bookmark_id)!r}. A bookmark id is not transferable"
            f" between conversations, so one taken from elsewhere names nothing"
            f" here.",
        )

    @staticmethod
    def _write_result(
        chat_id: str,
        action: str,
        supplied: Mapping[str, str | None],
        response: Mapping[str, Any],
    ) -> str:
        """Include Slack's bookmark after add/edit or the removed id."""
        payload: dict[str, Any] = {"ok": True, "chat_id": chat_id, "action": action}
        bookmark = response.get("bookmark")
        if isinstance(bookmark, Mapping):
            record = _normalize_bookmark(bookmark)
            if record.get("bookmark_id"):
                payload["bookmark_id"] = record["bookmark_id"]
            payload["bookmark"] = record
        else:
            if supplied["bookmark_id"]:
                payload["bookmark_id"] = supplied["bookmark_id"]
            if supplied["quip_section_id"]:
                payload["quip_section_id"] = supplied["quip_section_id"]
        logger.info("slack bookmark: %s in %s", action, chat_id)
        return json.dumps(payload, ensure_ascii=False)

    @checks_slack_ids
    async def read_bookmarks(self, chat_id: str | None = None) -> str:
        """Return every bookmark in an authorized conversation."""
        metadata = self._runtime_metadata()
        origin_id = self._chat_id(metadata)
        if not origin_id:
            return _read_refusal_json(
                "trusted_slack_channel_context_required",
                "this request carries no Slack conversation, so there is no"
                " bookmark bar to read and nothing to fall back to",
            )

        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _read_refusal_json(unresolved.code, unresolved.detail, chat_id=origin_id)
        target_id = str(chat_id or "").strip() or origin_id
        if target_id != origin_id:
            gate = SlackHistoryToolkit(metadata=dict(metadata), client=self._client, workspaces=self._workspaces)
            try:
                await gate.authorize_read_target(metadata, target_id)
            except _HistoryRefused as exc:
                return _read_refusal_json(exc.code, exc.detail, chat_id=origin_id)
        try:
            listing = await self._call(_BOOKMARK_LIST, channel_id=target_id)
        except _SlackCallFailure as exc:
            return _read_refusal_json(exc.code, self._failure_detail(exc), chat_id=target_id)

        records = [_normalize_bookmark(item) for item in (listing.get("bookmarks") or []) if isinstance(item, Mapping)]
        logger.info(
            "slack bookmarks: listed %d in %s",
            len(records),
            target_id,
        )
        return json.dumps(
            {
                "ok": True,
                "chat_id": target_id,
                "bookmarks_returned": len(records),
                "bookmarks": records,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
