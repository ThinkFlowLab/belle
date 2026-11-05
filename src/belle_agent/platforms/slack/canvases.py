"""Manage Slack canvases."""

import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from belle_agent.tools.inputs import string_ids as _string_list

from .history import SlackHistoryToolkit
from .history_policy import (
    SlackWorkspaceUnresolved,
)
from .json_toolkit import (
    ACCESS_PARAMS,
    ACCESS_PARTIAL_PROSE,
    ACCESS_TARGETS_PROSE,
    SHARED_FAILURE_DETAIL,
    SlackAccessRequest,
    SlackJsonToolkitBase,
    access_level_error,
    access_targets,
    owner_error,
    slack_failure_detail,
    slack_file_id_error,
    slack_no_request_detail,
    slack_refusal_json,
)
from .json_toolkit import (
    SlackJsonCallFailure as _CanvasCallFailure,
)
from .tool_context import CONVERSATION_ID_RE, SlackToolRefused, checks_slack_ids

logger = logging.getLogger(__name__)

CANVAS_TOOL_NAMES: tuple[str, ...] = (
    "read_slack_canvas",
    "write_slack_canvas",
    "write_slack_channel_canvas",
    "edit_slack_canvas",
    "share_slack_canvas",
    "delete_slack_canvas",
)

_CANVAS_CREATE = "canvases.create"
_CHANNEL_CANVAS_CREATE = "conversations.canvases.create"
_CANVAS_EDIT = "canvases.edit"
_CANVAS_DELETE = "canvases.delete"
_SLACK_TARGET_ARGUMENT: Mapping[str, str] = {
    "chat_ids": "channel_ids",
    "user_ids": "user_ids",
}

_CANVAS_ACCESS_SET = "canvases.access.set"
_CANVAS_ACCESS_DELETE = "canvases.access.delete"
_CANVAS_SECTIONS_LOOKUP = "canvases.sections.lookup"
_CANVAS_GET_CONTENT = "canvases.getContent"

_FILES_INFO = "files_info"

_CANVAS_READ_SCOPE = "canvases:read"
_CANVAS_WRITE_SCOPE = "canvases:write"

SECTION_TYPES: tuple[str, ...] = (
    "any_header",
    "blockquote",
    "callout",
    "canvas_unfurl",
    "chart",
    "citation",
    "file_unfurl",
    "flexbox",
    "h1",
    "h2",
    "h3",
    "horizontal_line",
    "list",
    "message_unfurl",
    "sfdc_record_mention",
    "sfdc_record_unfurl",
    "table",
    "user_mention",
    "user_unfurl",
)

_EDIT_OPERATIONS: Mapping[str, tuple[str, str]] = {
    "insert_after": ("required", "markdown"),
    "insert_before": ("required", "markdown"),
    "insert_at_start": ("forbidden", "markdown"),
    "insert_at_end": ("forbidden", "markdown"),
    "replace": ("optional", "markdown"),
    "delete": ("required", ""),
    "rename": ("forbidden", "title"),
}

MAX_MARKDOWN_CHARS = 1_048_576

MAX_CONTENT_CHARS = 60_000

DEFAULT_SECTIONS_MAX_ITEMS = 500


_FAILURE_DETAIL: Mapping[str, str] = {
    **SHARED_FAILURE_DETAIL,
    "canvas_not_found": (
        "Slack cannot distinguish a canvas that does not exist, was deleted,"
        " or has never been shared with this app. Ask someone to share an"
        " existing canvas; retrying alone will not help"
    ),
    "canvas_deleted": ("this canvas was deleted. Slack cannot restore it or accept further edits"),
    "canvas_editing_locked": (
        "another edit is running. This one failed and nothing changed; retry after a short delay"
    ),
    "canvas_editing_failed": ("Slack accepted the edit but could not apply it. Check the markdown or retry"),
    "canvas_too_large": ("the canvas or edit must be smaller. Retrying the same edit will fail"),
    "canvas_creation_failed": ("Slack accepted the request but did not create the canvas"),
    "canvas_tab_creation_failed": ("the canvas was created but could not be tabbed into the conversation"),
    "channel_canvas_creation_failed": ("Slack accepted the request but did not create the channel canvas"),
    "channel_canvas_already_exists": (
        "a conversation holds canvases one at a time; edit the existing canvas."
        " Deleting it is permanent, and creating a replacement is unverified"
    ),
    "free_teams_cannot_create_standalone_canvases": (
        "free workspaces require a conversation for each canvas. Pass chat_id"
    ),
    "free_teams_cannot_edit_standalone_canvases": ("free workspaces cannot edit standalone canvases. Nothing changed"),
    "free_team_canvas_tab_already_exists": ("free workspaces allow one canvas tab per conversation; this one has one"),
    "team_tier_cannot_create_channel_canvases": ("this workspace plan does not allow channel canvases"),
    "canvas_disabled_user_team": ("canvases are disabled in this workspace. Ask an administrator"),
    "canvas_globally_disabled": ("canvases are disabled across this Slack organization. Ask an administrator"),
    "canvas_deleting_disabled": ("canvas deletion is disabled in this workspace. The canvas is untouched"),
    "canvas_disabled_file_team": ("canvases are disabled in the workspace that owns this canvas"),
    "invalid_parameters": (
        "Slack expected exactly one of user_ids or channel_ids. If you named"
        " exactly one target set, report this as a tool fault"
    ),
    "failed_to_update_user_ids": (
        "Slack refused at least one user. Send the canvas to each user directly"
        " before setting access; conversation membership does not count"
    ),
}


def _scope_for(method: str) -> str:
    """Return the scope the refused canvas method needs."""
    return _CANVAS_READ_SCOPE if method in {_CANVAS_SECTIONS_LOOKUP, _CANVAS_GET_CONTENT} else _CANVAS_WRITE_SCOPE


def _canvas_refusal_json(
    code: str,
    detail: str = "",
    **fields: Any,
) -> str:
    return slack_refusal_json(logger, "canvas", code, detail, **fields)


class SlackCanvasToolkit(SlackJsonToolkitBase[_CanvasCallFailure]):
    active_turn_only = frozenset({"share_slack_canvas"})

    async def widening_question(self, tool_name: str, arguments: Mapping[str, Any] | None) -> str | None:
        if tool_name != "share_slack_canvas" or not isinstance(arguments, Mapping):
            return None
        kind, ids, refusal = self._access_targets(arguments.get("chat_ids"), arguments.get("user_ids"))
        if refusal or not ids:
            return None
        action = "Revoke" if arguments.get("remove") is True else "Grant"
        canvas_id = str(arguments.get("canvas_id") or "")
        targets = ", ".join(ids)
        if action == "Revoke":
            return f"Revoke access to Slack canvas {canvas_id} for {kind.replace('_', ' ')} {targets}?"
        level = str(arguments.get("access_level") or "")
        return f"Grant {level} access to Slack canvas {canvas_id} for {kind.replace('_', ' ')} {targets}?"

    @staticmethod
    def get_tool_definitions() -> list[dict[str, Any]]:
        return [
            _read_tool_definition(),
            _write_tool_definition(),
            _channel_write_tool_definition(),
            _edit_tool_definition(),
            _share_tool_definition(),
            _delete_tool_definition(),
        ]

    """The six canvas tools, scoped to the Slack install the request came from."""

    _failure_type = _CanvasCallFailure

    @staticmethod
    def _canvas_id_refusal(canvas_id: str) -> str | None:
        """Why *canvas_id* cannot be sent, or ``None`` when it can."""
        if problem := slack_file_id_error(canvas_id, "canvas"):
            return _canvas_refusal_json(*problem, canvas_id=canvas_id[:32])
        return None

    @staticmethod
    def _markdown_refusal(markdown: str, *, where: str) -> str | None:
        """Why *markdown* cannot be sent, or ``None`` when it can."""
        if len(markdown) <= MAX_MARKDOWN_CHARS:
            return None
        return _canvas_refusal_json(
            "content_too_large",
            f"Slack limits the markdown content of {where} to"
            f" {MAX_MARKDOWN_CHARS} characters and this came to"
            f" {len(markdown)}. Nothing was sent. Split the content across"
            f" several calls, or write less",
        )

    async def _settings_refusal(self, metadata: Mapping[str, Any], canvas_id: str = "") -> str | None:
        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            fields = {"canvas_id": canvas_id} if canvas_id else {}
            return _canvas_refusal_json(unresolved.code, unresolved.detail, **fields)
        return None

    def _slack_refusal(self, exc: _CanvasCallFailure, **fields: Any) -> str:
        """Keep Slack's line-specific detail for markdown errors."""
        detail = slack_failure_detail(exc, _FAILURE_DETAIL, scope=_scope_for(exc.method))
        if exc.detail:
            detail = f"{detail}. Slack said: {exc.detail}"
        return _canvas_refusal_json(exc.code, detail, **fields)

    async def _canvas_file(self, canvas_id: str) -> dict[str, Any]:
        """The ``files.info`` record for a canvas, or the refusal it earns."""
        response = await self._call(_FILES_INFO, file=canvas_id)
        record = response.get("file")
        if not isinstance(record, Mapping):
            raise _CanvasCallFailure("canvas_not_found", _FILES_INFO)
        return dict(record)

    async def _sections(self, canvas_id: str, criteria: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Return section identifiers; lookup does not include text."""
        response = await self._api(
            _CANVAS_SECTIONS_LOOKUP,
            {"canvas_id": canvas_id, "criteria": dict(criteria)},
        )
        raw = response.get("sections")
        out: list[dict[str, Any]] = []
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, Mapping):
                section_id = str(item.get("id") or "").strip()
                if section_id:
                    out.append({"section_id": section_id})
        return out

    @checks_slack_ids
    async def read_slack_canvas(
        self,
        canvas_id: str,
        section_types: Any = None,
        contains_text: str | None = None,
        content_format: str = "markdown",
    ) -> str:
        """Read one canvas and, if requested, its section identifiers."""
        metadata = self._runtime_metadata()
        if not metadata:
            return self._no_slack_request_refusal()
        raw_id = str(canvas_id or "").strip()
        refusal = self._canvas_id_refusal(raw_id)
        if refusal:
            return refusal

        types = _string_list(section_types)
        if types is None:
            return _canvas_refusal_json(
                "section_types_malformed",
                "section_types is a list of section type names. It was not a list of strings",
                canvas_id=raw_id,
            )
        unknown = [name for name in types if name not in SECTION_TYPES]
        if unknown:
            return _canvas_refusal_json(
                "section_type_unknown",
                f"Slack filters on {len(SECTION_TYPES)} section types and"
                f" {', '.join(sorted(unknown))} is not among them. Dropping"
                f" section_types and filtering on contains_text alone is the"
                f" only way to reach the types Slack does not let a query"
                f" filter on",
                canvas_id=raw_id,
            )
        text = str(contains_text or "").strip()
        if content_format not in {"markdown", "html"}:
            return _canvas_refusal_json(
                "content_format_invalid",
                "content_format must be markdown or html",
                canvas_id=raw_id,
            )

        try:
            settings = await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _canvas_refusal_json(unresolved.code, unresolved.detail, canvas_id=raw_id)
        max_chars = settings.get("canvas_read_max_chars", MAX_CONTENT_CHARS)
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
            return _canvas_refusal_json(
                "canvas_read_max_chars_invalid",
                "platforms.slack.canvas_read_max_chars must be a positive integer",
                canvas_id=raw_id,
            )
        max_sections = settings.get("canvas_sections_max_items", DEFAULT_SECTIONS_MAX_ITEMS)
        if type(max_sections) is not int or max_sections < 1:
            return _canvas_refusal_json(
                "canvas_sections_max_items_invalid",
                "platforms.slack.canvas_sections_max_items must be a positive integer",
                canvas_id=raw_id,
            )

        try:
            record = await self._canvas_file(raw_id)
        except _CanvasCallFailure as exc:
            return self._slack_refusal(exc, canvas_id=raw_id)

        audience = SlackHistoryToolkit(
            metadata=metadata, client=self._client, workspaces=self._workspaces,
        )
        try:
            await audience.authorize_file_record(record)
        except SlackToolRefused as refused:
            return _canvas_refusal_json(refused.code, refused.detail, canvas_id=raw_id)
        except Exception:  # noqa: BLE001 - a failed access check must disclose no content.
            return _canvas_refusal_json(
                "canvas_audience_unverified",
                "Belle could not verify that this conversation may read the canvas",
                canvas_id=raw_id,
            )

        filetype = str(record.get("filetype") or "").strip().lower()
        if filetype and filetype not in {"canvas", "quip"}:
            return _canvas_refusal_json(
                "not_a_canvas",
                f"this id names a file of type {filetype!r} rather than a canvas. Nothing was read",
                canvas_id=raw_id,
            )

        try:
            response = await self._api(
                _CANVAS_GET_CONTENT,
                {"canvas_id": raw_id, "content_type": content_format},
            )
        except _CanvasCallFailure as exc:
            return self._slack_refusal(exc, canvas_id=raw_id)
        content = response.get("content")
        if not isinstance(content, str):
            return _canvas_refusal_json(
                "canvas_content_missing",
                "Slack returned no canvas content string",
                canvas_id=raw_id,
            )
        if len(content) > max_chars:
            return _canvas_refusal_json(
                "canvas_content_too_large",
                f"canvas content has {len(content)} characters;"
                f" platforms.slack.canvas_read_max_chars is {max_chars}."
                " Raise that setting to read the whole canvas",
                canvas_id=raw_id,
            )

        result: dict[str, Any] = {
            "ok": True,
            "canvas_id": raw_id,
            "title": str(record.get("title") or ""),
            "is_channel_canvas": bool(record.get("is_channel_space")),
            "linked_chat_id": str(record.get("linked_channel_id") or ""),
            "permalink": str(record.get("permalink") or ""),
            "content_format": content_format,
            "content": content,
            "content_chars": len(content),
            "content_coverage": "complete",
        }

        coverage: list[str] = []
        criteria: dict[str, Any] = {}
        if types:
            criteria["section_types"] = types
        if text:
            criteria["contains_text"] = text
        if not criteria:
            # Slack rejects an empty criteria object; no filter means no lookup.
            result["sections_filter"] = "none"
            coverage.append(
                "sections_not_requested: pass section_types or contains_text"
                " to list section ids"
            )
        else:
            try:
                sections = await self._sections(raw_id, criteria)
            except _CanvasCallFailure as exc:
                detail = _FAILURE_DETAIL.get(exc.code, "")
                if exc.code == "missing_scope":
                    detail = f"this installation does not hold the {_CANVAS_READ_SCOPE} scope"
                coverage.append(f"sections_unavailable: {exc.code}" + (f" -- {detail}" if detail else ""))
            else:
                result["sections"] = sections[:max_sections]
                result["sections_count"] = len(result["sections"])
                result["sections_total"] = len(sections)
                if len(sections) > max_sections:
                    coverage.append(
                        f"sections_truncated: {len(sections)} match, but platforms.slack.canvas_sections_max_items"
                        f" allows {max_sections} in one result"
                    )
                result["sections_filter"] = criteria

        if coverage:
            result["coverage"] = coverage
        return json.dumps(result, ensure_ascii=False)

    @staticmethod
    def _document_content(markdown: str) -> dict[str, Any]:
        """Wrap markdown in Slack's document-content format."""
        return {"type": "markdown", "markdown": markdown}

    @classmethod
    def _create_payload(cls, title: str | None, markdown: str, chat_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if heading := str(title or "").strip():
            payload["title"] = heading
        if markdown:
            payload["document_content"] = cls._document_content(markdown)
        if chat_id:
            payload["channel_id"] = chat_id
        return payload

    @staticmethod
    def _chat_id_refusal(chat_id: str, *, required: bool) -> str | None:
        if not chat_id and required:
            return _canvas_refusal_json("chat_id_required", "a channel canvas needs a conversation id")
        if chat_id and not CONVERSATION_ID_RE.match(chat_id):
            return _canvas_refusal_json(
                "chat_id_malformed",
                "a Slack conversation id starts with C, D or G, then uses upper-case letters and digits",
                chat_id=chat_id[:32],
            )
        if chat_id.startswith("D"):
            return _canvas_refusal_json(
                "chat_id_is_a_direct_message",
                "a canvas cannot be tabbed into a direct message; share it with a user instead",
                chat_id=chat_id,
            )
        return None

    def _no_slack_request_refusal(self) -> str:
        """Refuse calls without a trusted Slack request."""
        return _canvas_refusal_json("trusted_slack_request_required", slack_no_request_detail("canvas"))

    async def _create(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        chat_id: str = "",
    ) -> str:
        """Make one canvas and report it, or say why it was not made."""
        try:
            response = await self._api(method, payload)
        except _CanvasCallFailure as exc:
            return self._slack_refusal(exc, chat_id=chat_id)
        canvas_id = str(response.get("canvas_id") or "").strip()
        logger.info("slack canvas: created %s via %s", canvas_id, method)
        result: dict[str, Any] = {"ok": True, "canvas_id": canvas_id}
        if chat_id:
            result["chat_id"] = chat_id
        result["is_channel_canvas"] = method == _CHANNEL_CANVAS_CREATE
        return json.dumps(result, ensure_ascii=False)

    @checks_slack_ids
    async def write_slack_canvas(
        self,
        title: str | None = None,
        markdown: str | None = None,
        chat_id: str | None = None,
    ) -> str:
        """Create a standalone canvas, optionally tabbed into a conversation."""
        metadata = self._runtime_metadata()
        if not metadata:
            return self._no_slack_request_refusal()

        body = str(markdown or "")
        refusal = self._markdown_refusal(body, where="a canvas")
        if refusal:
            return refusal
        room = str(chat_id or "").strip()
        if refusal := self._chat_id_refusal(room, required=False):
            return refusal

        if refusal := await self._settings_refusal(metadata):
            return refusal

        return await self._create(_CANVAS_CREATE, self._create_payload(title, body, room), chat_id=room)

    @checks_slack_ids
    async def write_slack_channel_canvas(
        self,
        chat_id: str,
        title: str | None = None,
        markdown: str | None = None,
    ) -> str:
        """Create the conversation's own canvas, which it may have only once."""
        metadata = self._runtime_metadata()
        if not metadata:
            return self._no_slack_request_refusal()

        room = str(chat_id or "").strip()
        if refusal := self._chat_id_refusal(room, required=True):
            return refusal
        body = str(markdown or "")
        refusal = self._markdown_refusal(body, where="a canvas")
        if refusal:
            return refusal

        if refusal := await self._settings_refusal(metadata):
            return refusal

        return await self._create(
            _CHANNEL_CANVAS_CREATE,
            self._create_payload(title, body, room),
            chat_id=room,
        )

    def _one_change(self, change: Any) -> tuple[dict[str, Any], str]:
        """Build one Slack change or return a refusal."""
        if not isinstance(change, Mapping):
            return {}, _canvas_refusal_json(
                "change_malformed",
                "each entry in changes is an object naming an operation and what it acts on. This one is not an object",
            )
        operation = str(change.get("operation") or "").strip()
        if operation not in _EDIT_OPERATIONS:
            return {}, _canvas_refusal_json(
                "operation_unknown",
                f"{operation!r} is not an edit operation. The operations are {', '.join(sorted(_EDIT_OPERATIONS))}",
            )
        section_rule, content_key = _EDIT_OPERATIONS[operation]
        section_id = str(change.get("section_id") or "").strip()
        if section_rule == "required" and not section_id:
            return {}, _canvas_refusal_json(
                "section_id_required",
                f"the {operation} operation acts on one section and none was"
                f" named. A section id is taken unchanged from a read of this"
                f" canvas",
            )
        if section_rule == "forbidden" and section_id:
            return {}, _canvas_refusal_json(
                "section_id_not_accepted",
                f"the {operation} operation does not act on a single section,"
                f" so a section id would be ignored. Nothing was sent",
            )

        body: dict[str, Any] = {"operation": operation}
        if section_id:
            body["section_id"] = section_id
        if not content_key:
            return body, ""

        markdown = str(change.get(content_key) or "")
        if operation == "rename":
            # Strip titles only; whitespace can change Markdown content.
            markdown = markdown.strip()
        if not markdown:
            return {}, _canvas_refusal_json(
                "change_content_required",
                f"the {operation} operation writes content and none was given. Pass it as {content_key}",
            )
        refusal = self._markdown_refusal(markdown, where="one change")
        if refusal:
            return {}, refusal
        # Slack uses title_content for rename, document_content otherwise.
        key = "title_content" if operation == "rename" else "document_content"
        body[key] = self._document_content(markdown)
        return body, ""

    @checks_slack_ids
    async def edit_slack_canvas(self, canvas_id: str, changes: Any) -> str:
        """Apply one change to a canvas."""
        metadata = self._runtime_metadata()
        if not metadata:
            return self._no_slack_request_refusal()
        raw_id = str(canvas_id or "").strip()
        refusal = self._canvas_id_refusal(raw_id)
        if refusal:
            return refusal

        if not isinstance(changes, Sequence) or isinstance(changes, (str, bytes)):
            return _canvas_refusal_json(
                "changes_malformed",
                "changes is a list of change objects. It was not a list",
                canvas_id=raw_id,
            )
        if not changes:
            return _canvas_refusal_json(
                "changes_required",
                "changes held nothing, so there is nothing to apply and the canvas is untouched",
                canvas_id=raw_id,
            )
        if len(changes) > 1:
            # Slack currently accepts one operation per call.
            return _canvas_refusal_json(
                "too_many_changes",
                f'Slack says of this method that "Only one operation per API'
                f' call is currently supported", and {len(changes)} were'
                f" given. Nothing was sent. Apply them one call at a time, in"
                f" the order they should take effect",
                canvas_id=raw_id,
            )

        body, change_refusal = self._one_change(changes[0])
        if change_refusal:
            return change_refusal

        if refusal := await self._settings_refusal(metadata, raw_id):
            return refusal

        try:
            await self._api(_CANVAS_EDIT, {"canvas_id": raw_id, "changes": [body]})
        except _CanvasCallFailure as exc:
            return self._slack_refusal(exc, canvas_id=raw_id)

        logger.info("slack canvas: applied %s to %s", body["operation"], raw_id)
        return json.dumps(
            {
                "ok": True,
                "canvas_id": raw_id,
                "operation": body["operation"],
                "section_id": body.get("section_id", ""),
            },
            ensure_ascii=False,
        )

    def _access_targets(self, chat_ids: Any, user_ids: Any) -> tuple[str, list[str], str]:
        kind, ids, refusal = access_targets(chat_ids, user_ids, reject_dms=True)
        if refusal:
            return "", [], _canvas_refusal_json(*refusal)
        return kind, ids, ""

    @checks_slack_ids
    async def share_slack_canvas(
        self,
        canvas_id: str,
        access_level: str | None = None,
        chat_ids: Any = None,
        user_ids: Any = None,
        remove: bool = False,
    ) -> str:
        """Set or revoke access to a canvas for conversations or for people."""
        metadata = self._runtime_metadata()
        if not metadata:
            return self._no_slack_request_refusal()
        raw_id = str(canvas_id or "").strip()
        refusal = self._canvas_id_refusal(raw_id)
        if refusal:
            return refusal

        level = str(access_level or "").strip().lower()
        if error := access_level_error(level, remove):
            return _canvas_refusal_json(*error, canvas_id=raw_id)

        kind, ids, target_refusal = self._access_targets(chat_ids, user_ids)
        if target_refusal:
            return target_refusal

        if error := owner_error(level, kind, len(ids), resource="canvas"):
            return _canvas_refusal_json(*error, canvas_id=raw_id)

        if refusal := await self._settings_refusal(metadata, raw_id):
            return refusal

        request = SlackAccessRequest(
            method=_CANVAS_ACCESS_DELETE if remove else _CANVAS_ACCESS_SET,
            resource_key="canvas_id",
            resource_id=raw_id,
            kind=kind,
            wire_kind=_SLACK_TARGET_ARGUMENT[kind],
            ids=tuple(ids),
            level=level,
            removes=remove,
        )
        return await self._share_access(
            request,
            logger=logger,
            details=_FAILURE_DETAIL,
            refusal=lambda exc: self._slack_refusal(
                exc,
                canvas_id=raw_id,
                **{kind: list(ids)},
                **self._sharing_hint(exc, kind, remove=remove),
            ),
            extra=lambda exc: self._sharing_hint(exc, kind, remove=remove),
        )

    @staticmethod
    def _sharing_hint(exc: _CanvasCallFailure, kind: str, *, remove: bool) -> dict[str, Any]:
        """Suggest a permalink for user-access failures or explain channel access."""
        if remove or kind != "user_ids":
            if exc.code == "canvas_not_found":
                return {
                    "next_step": (
                        "If this canvas belongs to a conversation, its access"
                        " follows from who is in that conversation and cannot"
                        " be set here at all."
                    )
                }
            return {}
        if exc.code not in {
            "canvas_not_found",
            "failed_to_update_user_ids",
            "user_not_found",
        }:
            return {}
        return {
            "next_step": (
                "A person's access can only be set on a canvas that has already"
                " been sent to them directly; being in a conversation the"
                " canvas was shared into does not count. Send them the canvas"
                " permalink in a message first, then set the level. If this is"
                " a conversation's own canvas, access follows membership and"
                " cannot be set here at all."
            )
        }

    @checks_slack_ids
    async def delete_slack_canvas(self, canvas_id: str) -> str:
        """Delete one canvas, permanently."""
        metadata = self._runtime_metadata()
        if not metadata:
            return self._no_slack_request_refusal()
        raw_id = str(canvas_id or "").strip()
        refusal = self._canvas_id_refusal(raw_id)
        if refusal:
            return refusal

        if refusal := await self._settings_refusal(metadata, raw_id):
            return refusal

        try:
            await self._api(_CANVAS_DELETE, {"canvas_id": raw_id})
        except _CanvasCallFailure as exc:
            return self._slack_refusal(exc, canvas_id=raw_id)

        logger.info("slack canvas: deleted %s", raw_id)
        return json.dumps(
            {"ok": True, "canvas_id": raw_id, "deleted": True},
            ensure_ascii=False,
        )

def _read_tool_definition() -> dict[str, Any]:
    return {
        "name": "read_slack_canvas",
        "description": (
            "Read a Slack canvas as markdown or HTML. The default is markdown. "
            "Belle refuses content above platforms.slack.canvas_read_max_chars; "
            "it never returns a shortened document. Pass section_types or "
            "contains_text to list matching section ids. The operator can raise "
            "platforms.slack.canvas_sections_max_items to return more section ids. A missing canvas may "
            "be absent, deleted, or unavailable to this request."
        ),
        "input_params": {
            "type": "object",
            "properties": {
                "canvas_id": {
                    "type": "string",
                    "description": (
                        "The canvas to read, such as F1234ABCD. Copy it verbatim from a result that reported one."
                    ),
                },
                "section_types": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(SECTION_TYPES)},
                    "description": (
                        "List only sections of these kinds. Leave it out and "
                        "pass contains_text instead to reach sections of the "
                        "kinds that cannot be filtered on."
                    ),
                },
                "contains_text": {
                    "type": "string",
                    "description": (
                        "List only sections holding this text. Combines with section_types when both are given."
                    ),
                },
                "content_format": {
                    "type": "string",
                    "enum": ["markdown", "html"],
                    "description": "Format for the full canvas content. Default: markdown.",
                },
            },
            "required": ["canvas_id"],
        },
    }


_CANVAS_TITLE_PARAM = {
    "type": "string",
    "description": ("Optional title. Omit for an untitled canvas. A title can be added later, but cannot be removed."),
}


_CANVAS_MARKDOWN_PARAM = {
    "type": "string",
    "description": f"Canvas markdown, at most {MAX_MARKDOWN_CHARS} characters.",
}


def _write_tool_definition() -> dict[str, Any]:
    return {
        "name": "write_slack_canvas",
        "description": (
            "Create an app-owned Slack canvas in markdown. It is private "
            "until shared, and creation notifies nobody. Block Kit is not "
            "accepted. Title and content are optional."
            "\nPass chat_id to add it as a conversation tab. For the "
            "conversation's own canvas, use write_slack_channel_canvas. "
            "A free Slack plan requires chat_id."
        ),
        "input_params": {
            "type": "object",
            "properties": {
                "title": _CANVAS_TITLE_PARAM,
                "markdown": _CANVAS_MARKDOWN_PARAM,
                "chat_id": {
                    "type": "string",
                    "description": (
                        "Put the canvas on this conversation as a tab. "
                        "Optional on a paid plan and required on a free one. "
                        "A direct message cannot hold a canvas tab."
                    ),
                },
            },
            "required": [],
        },
    }


def _channel_write_tool_definition() -> dict[str, Any]:
    return {
        "name": "write_slack_channel_canvas",
        "description": (
            "Create a channel's own canvas, visible to its members. A channel "
            "has one of these at a time. Create one only on request. Edit an "
            "existing one instead of creating another. Deletion cannot be "
            "undone. Whether a channel accepts a replacement is untested."
            "\nwrite_slack_canvas creates a separate canvas tab. Access to "
            "this channel canvas follows membership. Content is markdown, "
            "not Block Kit. A direct message has no channel canvas."
        ),
        "input_params": {
            "type": "object",
            "properties": {
                "chat_id": {
                    "type": "string",
                    "description": (
                        "The channel whose canvas this is. It must be a "
                        "channel; a direct message has no canvas of its own."
                    ),
                },
                "title": _CANVAS_TITLE_PARAM,
                "markdown": _CANVAS_MARKDOWN_PARAM,
            },
            "required": ["chat_id"],
        },
    }


def _edit_tool_definition() -> dict[str, Any]:
    return {
        "name": "edit_slack_canvas",
        "description": (
            "Edit one canvas section, replace all content, or rename it. "
            "One change is allowed per call. Read the canvas to get section ids."
            "\ninsert_after and insert_before need a section_id. insert_at_start "
            "and insert_at_end do not. delete needs the section_id to remove. "
            "replace with section_id changes one section. Without it, replace "
            "replaces the entire canvas. Pass section_id unless a full "
            "replacement was requested."
            "\nrename needs a nonempty title and changes no content. Pass the "
            "new markdown content for other writes. Slack reports invalid "
            "markdown. Retry after another editor's change finishes."
        ),
        "input_params": {
            "type": "object",
            "properties": {
                "canvas_id": {
                    "type": "string",
                    "description": ("The canvas to change, such as F1234ABCD."),
                },
                "changes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "operation": {
                                "type": "string",
                                "enum": sorted(_EDIT_OPERATIONS),
                                "description": "What to do.",
                            },
                            "section_id": {
                                "type": "string",
                                "description": (
                                    "The section to act on. Required for "
                                    "insert_after, insert_before and delete. "
                                    "Optional for replace, where leaving it "
                                    "out replaces the whole canvas. Not "
                                    "accepted by the others."
                                ),
                            },
                            "markdown": {
                                "type": "string",
                                "description": ("The new content, for every operation except delete and rename."),
                            },
                            "title": {
                                "type": "string",
                                "description": (
                                    "The new title, for rename. Required "
                                    "there and refused blank: renaming a "
                                    "canvas to nothing is not something "
                                    "Slack can be asked to do, so it is "
                                    "declined here rather than attempted. "
                                    "The create tools take the same argument "
                                    "and do accept a blank one, because "
                                    "leaving it out there asks for an "
                                    "untitled canvas. At most "
                                    f"{MAX_MARKDOWN_CHARS} characters, the "
                                    "limit Slack sets on the content of one "
                                    "change."
                                ),
                            },
                        },
                        "required": ["operation"],
                    },
                    "description": (
                        "The change to apply, as a list holding exactly one "
                        "change. Slack accepts one operation per call."
                    ),
                },
            },
            "required": ["canvas_id", "changes"],
        },
    }


def _share_tool_definition() -> dict[str, Any]:
    return {
        "name": "share_slack_canvas",
        "description": (
            "Set who may read or write a canvas, or take that access away."
            "\nThis does not hand anybody the canvas. It adjusts the level of "
            "access on a canvas somebody can already reach. For a person in "
            "particular, Slack requires that the canvas has already been sent "
            "to them directly -- in a message, by its link -- before their "
            "level can be set at all; being in a conversation where the canvas "
            "was shared does not count. If that has not happened, send them "
            "the canvas link first. This tool will not send it."
            + ACCESS_TARGETS_PROSE
            + "\nA conversation's own canvas has no access to set. Who may read "
            "it follows from who is in the conversation, and a call naming one "
            "fails." + ACCESS_PARTIAL_PROSE
        ),
        "input_params": {
            "type": "object",
            "properties": {
                "canvas_id": {
                    "type": "string",
                    "description": ("The canvas whose access is being set, such as F1234ABCD."),
                },
                **ACCESS_PARAMS,
                "user_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "The people to set access for. Cannot be given "
                        "together with chat_ids. Each of them must already "
                        "have been sent the canvas directly."
                    ),
                },
            },
            "required": ["canvas_id"],
        },
    }


def _delete_tool_definition() -> dict[str, Any]:
    return {
        "name": "delete_slack_canvas",
        "description": (
            "Delete a canvas in Slack."
            '\nIt cannot be undone. Slack says of this method: "Once a canvas '
            'is deleted, there is no way to get it back". Whatever the canvas '
            "held is gone, along with every link anybody has to it, and "
            "nobody is told. Delete a canvas when somebody asked for it to be "
            "deleted."
            "\nThis deletes a conversation's own canvas as well. Slack "
            "documents no exception for one and a channel canvas has been "
            "deleted this way. A channel holds one at a time, and nobody has "
            "tried whether it accepts a new one afterwards, so a channel left "
            "without its canvas may stay that way. Editing it empty is the "
            "way to clear one that can be undone."
            "\nDeleting can be switched off for a whole workspace, in which "
            "case this fails and says so, and nothing but an administrator "
            "change will alter that."
        ),
        "input_params": {
            "type": "object",
            "properties": {
                "canvas_id": {
                    "type": "string",
                    "description": ("The canvas to delete, such as F1234ABCD."),
                },
            },
            "required": ["canvas_id"],
        },
    }
