"""Read, write, share, and delete Slack Lists."""

import json
import logging
from collections.abc import Mapping
from typing import Any

import httpx

from belle_agent.tools.inputs import object_list as _object_list
from belle_agent.tools.inputs import string_ids as _string_list

from .file_transfer import (
    SLACK_FILE_HOST,
    SlackFileTransferRefused,
    https_file_host,
)
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
    slack_text_download,
)
from .json_toolkit import (
    SlackJsonCallFailure as _ListCallFailure,
)
from .rich_text import rich_text_block
from .tool_context import checks_slack_ids

logger = logging.getLogger(__name__)

LIST_TOOL_NAMES: tuple[str, ...] = (
    "read_slack_list",
    "write_slack_list",
    "edit_slack_list",
    "share_slack_list",
    "delete_slack_list",
)

_LIST_CREATE = "slackLists.create"
_LIST_UPDATE = "slackLists.update"
_LIST_ACCESS_SET = "slackLists.access.set"
_LIST_ACCESS_DELETE = "slackLists.access.delete"
_ITEMS_CREATE = "slackLists.items.create"
_ITEMS_INFO = "slackLists.items.info"
_ITEMS_LIST = "slackLists.items.list"
_ITEMS_UPDATE = "slackLists.items.update"
_ITEMS_DELETE = "slackLists.items.delete"
_ITEMS_DELETE_MULTIPLE = "slackLists.items.deleteMultiple"
_DOWNLOAD_START = "slackLists.download.start"
_DOWNLOAD_GET = "slackLists.download.get"

_FILES_INFO = "files_info"
_FILES_DELETE = "files_delete"

_LISTS_READ_SCOPE = "lists:read"
_LISTS_WRITE_SCOPE = "lists:write"

_READ_METHODS = frozenset({_ITEMS_INFO, _ITEMS_LIST, _DOWNLOAD_START, _DOWNLOAD_GET})


EXPORT_FORMATS: tuple[str, ...] = ("csv", "json")

COLUMN_TYPES: tuple[str, ...] = (
    "text",
    "rich_text",
    "message",
    "number",
    "select",
    "multi_select",
    "date",
    "user",
    "attachment",
    "checkbox",
    "email",
    "phone",
    "channel",
    "rating",
    "vote",
    "assignee",
    "due_date",
    "completed",
    "canvas",
    "link",
    "todo_completed",
    "todo_assignee",
    "todo_due_date",
)

CELL_VALUE_SHAPES: Mapping[str, str] = {
    "rich_text": (
        "an array holding one rich_text block, never a plain string --"
        " write the value as Markdown in text instead and this builds it"
    ),
    "select": (
        "an array of option ids. An option id is a choice's value in the"
        " column's schema, not the label printed beside it"
    ),
    "user": "an array of user ids, each a U or a W",
    "date": ("an array of one YYYY-MM-DD string. Not a Unix time, and not a bare string outside an array"),
    "checkbox": ("a bare true or false. The one documented value that is not in an array"),
    "number": "an array of numbers",
    "rating": "an array of one number",
    "email": "an array of email addresses",
    "phone": "an array of phone numbers, written as text",
    "channel": "an array of conversation ids, each a C",
    "message": "an array of message permalinks",
    "attachment": "an array of file ids, each an F",
    "link": ("an array of objects, each with original_url and optionally display_name and display_as_url"),
    "timestamp": "an array of one Unix time in seconds",
    "reference": ('an array of objects shaped {"file": {"file_id": "F1234ABCD"}}'),
}

UNDOCUMENTED_CELL_TYPES: tuple[str, ...] = (
    "multi_select",
    "vote",
    "canvas",
    "assignee",
    "due_date",
    "completed",
    "todo_completed",
    "todo_assignee",
    "todo_due_date",
)

EDIT_OPERATIONS: Mapping[str, str] = {
    "create_item": _ITEMS_CREATE,
    "update_cells": _ITEMS_UPDATE,
    "delete_item": _ITEMS_DELETE,
    "delete_items": _ITEMS_DELETE_MULTIPLE,
    "update_list": _LIST_UPDATE,
}

_EDIT_ARGUMENTS: Mapping[str, tuple[str, ...]] = {
    "create_item": ("initial_fields", "duplicated_item_id", "parent_item_id"),
    "update_cells": ("cells",),
    "delete_item": ("item_id",),
    "delete_items": ("item_ids",),
    "update_list": ("name", "description", "description_blocks", "todo_mode"),
}

MAX_ITEMS_PER_PAGE = 200

_MAX_EXPORT_POLLS = 3

_EXPORT_POLL_SECONDS = 1.0

MAX_EXPORT_BYTES = 4 * 1024 * 1024

MAX_EXPORT_CHARS = 60_000

_EXPORT_HOSTS = frozenset({SLACK_FILE_HOST})

_FAILURE_DETAIL: Mapping[str, str] = {
    **SHARED_FAILURE_DETAIL,
    "list_not_found": (
        "no List with this id is visible. It may not exist, may be deleted,"
        " or may need someone to share it with this app"
    ),
    "record_not_found": ("this List has no item with that id. Use an id from this List's items"),
    "record_deleted": ("this item was deleted and cannot be restored"),
    "row_not_found": ("this List has no row with that id. Use an id from this List"),
    "invalid_row_id": ("a cell names an invalid row id. Use an id from this List"),
    "row_id_not_provided": ("a cell needs row_id or row_id_to_create set to true"),
    "invalid_column_id": ("a cell names an unknown column id. Read an item for this List's schema"),
    "column_not_found": ("this List has no such column. Read an item for its column ids"),
    "column_id_not_provided": ("one of the cells names no column at all. Every cell needs a column_id"),
    "uneditable_column": ("this column is computed or managed by Slack and cannot be edited"),
    "invalid_input_type": (
        "a cell's value does not match its column type. Read the schema for the required key and value shape"
    ),
    "invalid_option_id": ("a select cell names an option absent from the column schema"),
    "invalid_vote_value": "a vote cell's value is not one this column accepts",
    "invalid_date": (
        "a date cell's value is not a date Slack accepts. Its documented"
        " example is an array holding one YYYY-MM-DD string"
    ),
    "invalid_email": "an email cell's value is not an email address",
    "invalid_phone_number": "a phone cell's value is not a phone number",
    "invalid_link": "a link cell's value is not a URL Slack will accept",
    "invalid_message": ("a message cell's value does not name a Slack message this app can see"),
    "invalid_blocks": (
        "a cell needs an array containing a valid rich_text block, not plain text or a bare elements array"
    ),
    "invalid_text_block": (
        "a text cell needs an array containing a rich_text block; Slack does not accept plain text on write"
    ),
    "invalid_attachment": "an attachment cell's value is not one Slack accepts",
    "invalid_schema": ("the schema is not a column definition Slack will accept. Nothing was created"),
    "invalid_column_type": ("the schema names a column type Slack does not have. Nothing was created"),
    "invalid_primary_column": (
        "the schema's primary column is not one Slack will accept as the primary. Nothing was created"
    ),
    "invalid_copy_and_schema_args": ("copying a List uses its columns. Pass copy_from_list_id or schema, not both"),
    "missing_arg_copy_from_list_id": (
        "include_copied_list_records says what to do with the records of a List"
        " being copied, and no List was named to copy. Pass copy_from_list_id"
    ),
    "unexpected_description_blocks_arg": ("Slack refused the description payload on this call"),
    "duplicated_item_not_found": ("the item to copy is not visible. Use an id from this List's items"),
    "over_row_maximum": ("this List reached Slack's row limit. Delete rows or use another List"),
    "over_column_maximum": ("the schema exceeds Slack's column limit. No List was created"),
    "over_cell_fields_limit": ("too many cells were sent. Split them across several calls"),
    "over_list_file_maximum": ("this workspace reached Slack's List limit. Delete a List to make room"),
    "over_title_length_maximum": ("the name is longer than Slack allows. Nothing was written"),
    "archive_not_supported": ("this List does not keep archived items, so there is no archived page to return"),
    "invalid_cursor": ("this cursor expired. Start the listing from the first page"),
    "job_not_found": ("the export job is absent or expired. Start a new export"),
    "permission_denied": ("this app lacks the required List access. Ask its owner to grant access"),
    "lists_disabled_user_team": ("Lists are disabled in this workspace. Ask an administrator"),
    "unknown_method": ("Slack reports that Lists are not enabled for this workspace plan"),
    "invalid_parameters": (
        "Slack read the targets as neither one thing nor the other. Exactly one"
        " of chat_ids and user_ids may be given, and one of them must be"
    ),
    "failed_to_update_user_ids": (
        "Slack refused the change for at least one of the named users and does not say which"
    ),
    "file_not_found": ("no file with this id is visible. Ask someone to share the List"),
    "file_deleted": "this file has already been deleted",
    "file_deleting_disabled": ("file deletion is disabled in this workspace. The List is untouched"),
    "cant_delete_file": ("this app lacks permission to delete this file"),
    "delete_not_allowed": (
        "Slack refuses to delete this file at all. Its documented example is a"
        " canvas template; whatever the reason, nothing was deleted"
    ),
    "invalid_arguments": (
        "Slack rejected the call's arguments and names none of them. Every"
        " value it saw is one this tool was given and forwarded unchanged"
    ),
    "export_host_not_allowed": ("the export URL is outside files.slack.com, so this app will not send its token there"),
    "export_read_denied": ("the export finished, but Slack denied its download"),
    "export_download_failed": ("the export finished, but its download failed. Retry with the job id"),
}

_CELL_WRITE_METHODS = frozenset({_ITEMS_CREATE, _ITEMS_UPDATE})

_INVALID_CELL_ARGUMENTS_DETAIL = (
    "Slack rejected the arguments for this cell write and names none of them,"
    " so this names what to check rather than what was wrong. Two causes"
    " produce it. A column_id that is the key a schema was written with rather"
    " than the id Slack generated for that column -- read the List with"
    " include_list true and take the id from the schema. Or a value in the"
    " wrong shape for its column type: almost every type takes its value in an"
    " array even where the column holds one thing, a text cell takes an array"
    " holding a rich_text block rather than a string, and checkbox is the one"
    " Slack documents as a bare boolean. A call whose column ids and value"
    " shapes are both right and that still gets this back is a fault in this"
    " tool rather than in the arguments, and is worth reporting as one"
)


def _scope_for(method: str) -> str:
    """Return the scope the refused List method needs."""
    return _LISTS_READ_SCOPE if method in _READ_METHODS else _LISTS_WRITE_SCOPE


def _list_refusal_json(code: str, detail: str = "", **fields: Any) -> str:
    return slack_refusal_json(logger, "list", code, detail, **fields)


def _cell_payload(cell: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    """Convert Markdown ``text`` to rich text. Pass other keys unchanged."""
    payload = {key: value for key, value in cell.items() if key != "text"}
    raw = cell.get("text")
    if raw is None:
        return payload, ""
    if not isinstance(raw, str):
        return {}, "a cell's text must be a string of Markdown"
    if "rich_text" in payload:
        return {}, (
            "a cell carries text or rich_text, not both: they are two spellings"
            " of one value and nothing here can say which was meant"
        )
    block = rich_text_block(raw)
    if block is None:
        return {}, ("a cell's text held nothing a reader would see. Write the text, or leave the cell out")
    payload["rich_text"] = [block]
    return payload, ""


class SlackListToolkit(SlackJsonToolkitBase[_ListCallFailure]):
    active_turn_only = frozenset({"share_slack_list"})

    async def widening_question(self, tool_name: str, arguments: Mapping[str, Any] | None) -> str | None:
        if tool_name != "share_slack_list" or not isinstance(arguments, Mapping):
            return None
        kind, ids, refusal = self._access_targets(arguments.get("chat_ids"), arguments.get("user_ids"))
        if refusal or not ids:
            return None
        action = "Revoke" if arguments.get("remove") is True else "Grant"
        list_id = str(arguments.get("list_id") or "")
        targets = ", ".join(ids)
        if action == "Revoke":
            return f"Revoke access to Slack List {list_id} for {kind.replace('_', ' ')} {targets}?"
        level = str(arguments.get("access_level") or "")
        return f"Grant {level} access to Slack List {list_id} for {kind.replace('_', ' ')} {targets}?"

    @staticmethod
    def get_tool_definitions() -> list[dict[str, Any]]:
        return [
            _read_tool_definition(),
            _write_tool_definition(),
            _edit_tool_definition(),
            _share_tool_definition(),
            _delete_tool_definition(),
        ]

    """The five List tools, scoped to the Slack install the request came from."""

    _failure_type = _ListCallFailure

    @staticmethod
    def _list_id_refusal(list_id: str) -> str | None:
        """Why *list_id* cannot be sent, or ``None`` when it can."""
        if problem := slack_file_id_error(list_id, "List"):
            return _list_refusal_json(*problem, list_id=list_id[:32])
        return None

    def _slack_refusal(self, exc: _ListCallFailure, **fields: Any) -> str:
        """Add method-specific detail to a Slack refusal."""
        if exc.code == "invalid_arguments" and exc.method in _CELL_WRITE_METHODS:
            detail = _INVALID_CELL_ARGUMENTS_DETAIL
        else:
            detail = slack_failure_detail(exc, _FAILURE_DETAIL, scope=_scope_for(exc.method))
        if exc.detail:
            detail = f"{detail}. Slack said: {exc.detail}"
        return _list_refusal_json(exc.code, detail, **fields)

    @staticmethod
    def _no_slack_request_refusal() -> str:
        """Refuse a request with no trusted Slack context."""
        return _list_refusal_json("trusted_slack_request_required", slack_no_request_detail("List"))

    def _description_refusal(
        self, description: Any, description_blocks: Any
    ) -> tuple[list[dict[str, Any]] | None, str]:
        """Return description blocks or a refusal. ``None`` means unchanged."""
        if description is not None and description_blocks is not None:
            return None, _list_refusal_json(
                "description_ambiguous",
                "description and description_blocks are two spellings of one"
                " value and nothing here can say which was meant. Pass one",
            )
        if description_blocks is not None:
            blocks = _object_list(description_blocks)
            if blocks is None:
                return None, _list_refusal_json(
                    "description_blocks_malformed",
                    "description_blocks is a list of Block Kit blocks. It was not a list of objects",
                )
            return blocks, ""
        if description is None:
            return None, ""
        if not isinstance(description, str):
            return None, _list_refusal_json(
                "description_malformed",
                "description is a string of Markdown. It was not a string",
            )
        block = rich_text_block(description)
        return ([] if block is None else [block]), ""

    async def _settings_refusal(self, metadata: Mapping[str, Any], list_id: str = "") -> str | None:
        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _list_refusal_json(unresolved.code, unresolved.detail, list_id=list_id)
        return None

    @checks_slack_ids
    async def read_slack_list(
        self,
        list_id: str,
        item_id: str | None = None,
        include_is_subscribed: bool = False,
        archived: bool = False,
        include_list: bool = False,
        limit: int | None = None,
        cursor: str | None = None,
        export: bool = False,
        export_format: str | None = None,
        export_job_id: str | None = None,
        include_archived: bool = False,
        include_threads: bool = False,
        include_attachments: bool = False,
    ) -> str:
        """Read a List: one item, a page of items, or an export of the whole."""
        metadata = self._runtime_metadata()
        if not metadata:
            return self._no_slack_request_refusal()
        raw_id = str(list_id or "").strip()
        refusal = self._list_id_refusal(raw_id)
        if refusal:
            return refusal

        item = str(item_id or "").strip()
        job = str(export_job_id or "").strip()
        wants_export = bool(export) or bool(job)
        if item and wants_export:
            return _list_refusal_json(
                "read_mode_ambiguous",
                "item_id reads one item and export exports the whole List."
                " They are two different calls; make one of them",
                list_id=raw_id,
            )

        if wants_export:
            return await self._export(
                metadata,
                raw_id,
                job=job,
                export_format=export_format,
                include_archived=bool(include_archived),
                include_threads=bool(include_threads),
                include_attachments=bool(include_attachments),
            )

        misplaced = [
            name
            for name, given in (
                ("export_format", export_format is not None),
                ("include_archived", bool(include_archived)),
                ("include_threads", bool(include_threads)),
                ("include_attachments", bool(include_attachments)),
            )
            if given
        ]
        if misplaced:
            return _list_refusal_json(
                "export_arguments_without_export",
                f"{', '.join(misplaced)} only affect an export and no export was asked for. Pass export to start one",
                list_id=raw_id,
            )

        if item:
            stray = [
                name
                for name, given in (
                    ("archived", bool(archived)),
                    ("include_list", bool(include_list)),
                    ("limit", limit is not None),
                    ("cursor", cursor is not None),
                )
                if given
            ]
            if stray:
                return _list_refusal_json(
                    "listing_arguments_with_item_id",
                    f"{', '.join(stray)} page through a List and item_id reads"
                    f" one item. Reading one item returns the parent List"
                    f" anyway -- its schema, its views and its limits -- so"
                    f" neither is needed alongside the other",
                    list_id=raw_id,
                )
            return await self._read_item(
                metadata,
                raw_id,
                item,
                include_is_subscribed=bool(include_is_subscribed),
            )

        if include_is_subscribed:
            return _list_refusal_json(
                "include_is_subscribed_without_item_id",
                "include_is_subscribed says whether one item is subscribed to, so it needs the item. Pass item_id",
                list_id=raw_id,
            )
        return await self._read_page(
            metadata,
            raw_id,
            archived=bool(archived),
            include_list=bool(include_list),
            limit=limit,
            cursor=cursor,
        )

    async def _read_item(
        self,
        metadata: Mapping[str, Any],
        list_id: str,
        item_id: str,
        *,
        include_is_subscribed: bool,
    ) -> str:
        """One item, and the List object Slack folds in beside it."""
        if refusal := await self._settings_refusal(metadata, list_id):
            return refusal

        payload: dict[str, Any] = {"list_id": list_id, "id": item_id}
        if include_is_subscribed:
            payload["include_is_subscribed"] = True
        try:
            response = await self._api(_ITEMS_INFO, payload)
        except _ListCallFailure as exc:
            return self._slack_refusal(exc, list_id=list_id, item_id=item_id)

        result: dict[str, Any] = {
            "ok": True,
            "list_id": list_id,
            "item_id": item_id,
            "item": response.get("record") or response.get("item") or {},
            "list_note": (
                "Reading one item returns the List it belongs to as well: its"
                " column schema, its views and its limits. Slack has no method"
                " that returns a List on its own."
            ),
        }
        for key in ("list", "subtask_schema", "views", "limits"):
            if key in response:
                result[key] = response[key]
        if include_is_subscribed and "is_subscribed" in response:
            result["is_subscribed"] = response["is_subscribed"]
        return json.dumps(result, ensure_ascii=False)

    async def _read_page(
        self,
        metadata: Mapping[str, Any],
        list_id: str,
        *,
        archived: bool,
        include_list: bool,
        limit: Any,
        cursor: Any,
    ) -> str:
        """One page of items, and the cursor that continues it."""
        if limit is not None and not isinstance(limit, int):
            return _list_refusal_json(
                "limit_malformed",
                "limit is a whole number of rows. It was not a number",
                list_id=list_id,
            )
        if isinstance(limit, int) and limit < 1:
            return _list_refusal_json(
                "limit_malformed",
                f"limit is a whole number of rows and {limit} is not one",
                list_id=list_id,
            )
        if cursor is not None and not isinstance(cursor, str):
            return _list_refusal_json(
                "cursor_malformed",
                "cursor is the string a previous page reported. It was not a string",
                list_id=list_id,
            )

        if refusal := await self._settings_refusal(metadata, list_id):
            return refusal

        asked = min(int(limit), MAX_ITEMS_PER_PAGE) if limit else MAX_ITEMS_PER_PAGE
        payload: dict[str, Any] = {"list_id": list_id, "limit": asked}
        if archived:
            payload["archived"] = True
        if include_list:
            payload["include_list"] = True
        page = str(cursor or "").strip()
        if page:
            payload["cursor"] = page

        try:
            response = await self._api(_ITEMS_LIST, payload)
        except _ListCallFailure as exc:
            return self._slack_refusal(exc, list_id=list_id)

        raw = response.get("items")
        if not isinstance(raw, list):
            raw = response.get("records")
        items = [dict(row) for row in raw if isinstance(row, Mapping)] if isinstance(raw, list) else []
        result: dict[str, Any] = {
            "ok": True,
            "list_id": list_id,
            "items": items,
            "items_count": len(items),
            "archived": bool(archived),
        }
        if include_list and "list" in response:
            result["list"] = response["list"]
        next_cursor = ""
        meta = response.get("response_metadata")
        if isinstance(meta, Mapping):
            next_cursor = str(meta.get("next_cursor") or "").strip()
        if not next_cursor:
            next_cursor = str(response.get("next_cursor") or "").strip()
        coverage: list[str] = []
        if next_cursor:
            result["next_cursor"] = next_cursor
            coverage.append(
                "more_items: this is one page and there are more. Pass next_cursor back as cursor to continue"
            )
        if limit and int(limit) > MAX_ITEMS_PER_PAGE:
            coverage.append(
                f"limit_reduced: {int(limit)} rows were asked for and this tool"
                f" returns at most {MAX_ITEMS_PER_PAGE} in one page"
            )
        coverage.append(
            "no_server_side_query: Slack's listing takes only a page size, a"
            " cursor and the archived flag. There is no way to ask it for the"
            " rows where a column has a value, so filtering or sorting by a"
            " column means paging the whole List and doing it here"
        )
        result["coverage"] = coverage
        return json.dumps(result, ensure_ascii=False)

    async def _export_content(self, url: str, *, max_bytes: int, max_chars: int) -> tuple[str, int, bool]:
        """Fetch bounded export text without exposing its private URL."""
        if https_file_host(url) not in _EXPORT_HOSTS:
            raise _ListCallFailure("export_host_not_allowed", _DOWNLOAD_GET)
        try:
            status, content, total, truncated = await slack_text_download(
                url,
                bot_token=self._bot_token,
                allowed_hosts=_EXPORT_HOSTS,
                byte_limit=max_bytes,
                char_limit=max_chars,
                http_client_factory=self._http_client_factory,
            )
        except SlackFileTransferRefused as refused:
            raise _ListCallFailure(refused.code, _DOWNLOAD_GET) from None
        except httpx.HTTPError:
            raise _ListCallFailure("export_download_failed", _DOWNLOAD_GET) from None
        if status in (401, 403):
            raise _ListCallFailure("export_read_denied", _DOWNLOAD_GET)
        if status != 200:
            raise _ListCallFailure("export_download_failed", _DOWNLOAD_GET)
        return content, total, truncated

    async def _export(
        self,
        metadata: Mapping[str, Any],
        list_id: str,
        *,
        job: str,
        export_format: Any,
        include_archived: bool,
        include_threads: bool,
        include_attachments: bool,
    ) -> str:
        """Start or poll an export, repeating Slack's required options."""
        fmt = str(export_format or "csv").strip().lower()
        if fmt not in EXPORT_FORMATS:
            return _list_refusal_json(
                "export_format_unknown",
                f"{fmt!r} is not an export format. Slack's two are {', '.join(EXPORT_FORMATS)}",
                list_id=list_id,
            )
        if fmt != "json":
            extras = [
                name
                for name, given in (
                    ("include_threads", include_threads),
                    ("include_attachments", include_attachments),
                )
                if given
            ]
            if extras:
                return _list_refusal_json(
                    "export_option_needs_json",
                    f"Slack applies {' and '.join(extras)} only when the format"
                    f" is json, so a csv export would silently leave that out."
                    f" Pass export_format json, or drop the option",
                    list_id=list_id,
                )

        try:
            settings = await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _list_refusal_json(unresolved.code, unresolved.detail, list_id=list_id)
        max_bytes = int(settings.get("lists_export_max_bytes", MAX_EXPORT_BYTES))
        max_chars = int(settings.get("lists_export_max_chars", MAX_EXPORT_CHARS))

        job_id = job
        if not job_id:
            start: dict[str, Any] = {"list_id": list_id, "format": fmt}
            if include_archived:
                start["include_archived"] = True
            if include_threads:
                start["include_threads"] = True
            if include_attachments:
                start["include_attachments"] = True
            try:
                response = await self._api(_DOWNLOAD_START, start)
            except _ListCallFailure as exc:
                return self._slack_refusal(exc, list_id=list_id)
            job_id = str(response.get("job_id") or "").strip()
            if not job_id:
                return _list_refusal_json(
                    "export_job_missing",
                    "Slack accepted the export and named no job to ask after, so there is nothing to collect it with",
                    list_id=list_id,
                )

        ask: dict[str, Any] = {
            "list_id": list_id,
            "job_id": job_id,
            "format": fmt,
        }
        if include_threads:
            ask["include_threads"] = True
        if include_attachments:
            ask["include_attachments"] = True

        status = ""
        for attempt in range(_MAX_EXPORT_POLLS):
            if self._remaining() <= 0:
                break
            try:
                response = await self._api(_DOWNLOAD_GET, ask)
            except _ListCallFailure as exc:
                return self._slack_refusal(exc, list_id=list_id, export_job_id=job_id)
            status = str(response.get("status") or "").strip()
            url = str(response.get("download_url") or "").strip()
            if url:
                logger.info("slack list: exported %s as %s", list_id, fmt)
                return await self._exported(list_id, job_id, fmt, status, url, max_bytes=max_bytes, max_chars=max_chars)
            if attempt + 1 < _MAX_EXPORT_POLLS:
                await self._sleep(_EXPORT_POLL_SECONDS)

        return json.dumps(
            {
                "ok": True,
                "list_id": list_id,
                "export_job_id": job_id,
                "export_format": fmt,
                "status": status,
                "ready": False,
                "detail": (
                    "The export has been started and was not ready yet. Ask"
                    " again with export_job_id set to this job, and the same"
                    " export_format, include_threads and include_attachments --"
                    " Slack requires them to match the call that started it."
                ),
            },
            ensure_ascii=False,
        )

    async def _exported(
        self,
        list_id: str,
        job_id: str,
        fmt: str,
        status: str,
        url: str,
        *,
        max_bytes: int,
        max_chars: int,
    ) -> str:
        """Report a finished export and any content this tool could fetch."""
        result: dict[str, Any] = {
            "ok": True,
            "list_id": list_id,
            "export_job_id": job_id,
            "export_format": fmt,
            "status": status,
            "download_url": url,
        }
        try:
            content, total, truncated = await self._export_content(url, max_bytes=max_bytes, max_chars=max_chars)
        except _ListCallFailure as exc:
            result["content_error"] = exc.code
            result["download_note"] = (
                "The export finished and its content could not be fetched"
                f" here: {exc.code}. The URL is Slack's own, needs this app's"
                " token, and is not public."
            )
            return json.dumps(result, ensure_ascii=False)
        if truncated:
            result["content_error"] = "export_over_size_limit"
            result["observed_chars_at_least"] = total
            result["download_note"] = (
                f"The export exceeds platforms.slack.lists_export_max_chars ({max_chars})"
                f" or platforms.slack.lists_export_max_bytes ({max_bytes})."
                " No partial document was returned. Raise the relevant setting and retry with"
                " export_job_id; download_url is Slack's own and requires this app's token."
            )
        else:
            result["content"] = content
            result["content_chars"] = len(content)
            result["content_truncated"] = False
            result["download_note"] = (
                "The content above is the whole export. The URL is Slack's"
                " own, needs this app's token, and is not public."
            )
        return json.dumps(result, ensure_ascii=False)

    @checks_slack_ids
    async def write_slack_list(
        self,
        name: str,
        description: str | None = None,
        description_blocks: Any = None,
        schema: Any = None,
        copy_from_list_id: str | None = None,
        include_copied_list_records: bool = False,
        todo_mode: bool = False,
    ) -> str:
        """Create a List, from a schema or as a copy of an existing one."""
        metadata = self._runtime_metadata()
        if not metadata:
            return self._no_slack_request_refusal()

        title = str(name or "").strip()
        if not title:
            return _list_refusal_json(
                "name_required",
                "a List is created with a name and none was given",
            )

        blocks, refusal = self._description_refusal(description, description_blocks)
        if refusal:
            return refusal

        columns: list[dict[str, Any]] | None = None
        if schema is not None:
            columns = _object_list(schema)
            if columns is None:
                return _list_refusal_json(
                    "schema_malformed",
                    "schema is a list of column definitions. It was not a list of objects",
                )
            if not columns:
                return _list_refusal_json(
                    "schema_empty",
                    "schema held no columns. Leave it out to take Slack's"
                    " default of one text column, or name the columns",
                )

        source = str(copy_from_list_id or "").strip()
        if source:
            source_refusal = self._list_id_refusal(source)
            if source_refusal:
                return source_refusal
            if columns is not None:
                return _list_refusal_json(
                    "invalid_copy_and_schema_args",
                    _FAILURE_DETAIL["invalid_copy_and_schema_args"],
                )
        elif include_copied_list_records:
            return _list_refusal_json(
                "missing_arg_copy_from_list_id",
                _FAILURE_DETAIL["missing_arg_copy_from_list_id"],
            )

        if refusal := await self._settings_refusal(metadata):
            return refusal

        payload: dict[str, Any] = {"name": title}
        if blocks is not None:
            payload["description_blocks"] = blocks
        if columns is not None:
            payload["schema"] = columns
        if source:
            payload["copy_from_list_id"] = source
            if include_copied_list_records:
                payload["include_copied_list_records"] = True
        if todo_mode:
            payload["todo_mode"] = True

        try:
            response = await self._api(_LIST_CREATE, payload)
        except _ListCallFailure as exc:
            return self._slack_refusal(exc)

        created = response.get("list")
        new_id = ""
        if isinstance(created, Mapping):
            new_id = str(created.get("id") or created.get("list_id") or "").strip()
        if not new_id:
            new_id = str(response.get("list_id") or response.get("id") or "").strip()
        logger.info("slack list: created %s", new_id)
        result: dict[str, Any] = {
            "ok": True,
            "list_id": new_id,
            "name": title,
            "todo_mode": bool(todo_mode),
            "schema_note": (
                "The columns this List has are settled now. No Slack method"
                " adds, removes, renames or retypes a column afterwards, and"
                " editing a List covers its name, its description and todo mode"
                " only. Changing the columns means creating another List."
            ),
        }
        if isinstance(created, Mapping):
            result["list"] = dict(created)
        return json.dumps(result, ensure_ascii=False)

    @checks_slack_ids
    async def edit_slack_list(
        self,
        list_id: str,
        operation: str,
        cells: Any = None,
        initial_fields: Any = None,
        duplicated_item_id: str | None = None,
        parent_item_id: str | None = None,
        item_id: str | None = None,
        item_ids: Any = None,
        name: str | None = None,
        description: str | None = None,
        description_blocks: Any = None,
        todo_mode: Any = None,
    ) -> str:
        """Add an item, write cells, delete items, or rename the List."""
        metadata = self._runtime_metadata()
        if not metadata:
            return self._no_slack_request_refusal()
        raw_id = str(list_id or "").strip()
        refusal = self._list_id_refusal(raw_id)
        if refusal:
            return refusal

        action = str(operation or "").strip()
        if action not in EDIT_OPERATIONS:
            return _list_refusal_json(
                "operation_unknown",
                f"{action!r} is not an edit operation. The operations are {', '.join(sorted(EDIT_OPERATIONS))}",
                list_id=raw_id,
            )

        given = {
            "cells": cells is not None,
            "initial_fields": initial_fields is not None,
            "duplicated_item_id": bool(str(duplicated_item_id or "").strip()),
            "parent_item_id": bool(str(parent_item_id or "").strip()),
            "item_id": bool(str(item_id or "").strip()),
            "item_ids": item_ids is not None,
            "name": name is not None,
            "description": description is not None,
            "description_blocks": description_blocks is not None,
            "todo_mode": todo_mode is not None,
        }
        allowed = set(_EDIT_ARGUMENTS[action])
        stray = sorted(key for key, present in given.items() if present and key not in allowed)
        if stray:
            return _list_refusal_json(
                "argument_not_for_this_operation",
                f"{', '.join(stray)} {'do' if len(stray) > 1 else 'does'} not"
                f" belong to {action}, which takes"
                f" {', '.join(sorted(allowed))}. Nothing was sent, because an"
                f" argument this call would have ignored means the call was not"
                f" the one that was meant",
                list_id=raw_id,
            )

        builder = {
            "create_item": lambda: self._create_item_payload(
                raw_id, initial_fields, duplicated_item_id, parent_item_id
            ),
            "update_cells": lambda: self._update_cells_payload(raw_id, cells),
            "delete_item": lambda: self._delete_item_payload(raw_id, item_id),
            "delete_items": lambda: self._delete_items_payload(raw_id, item_ids),
            "update_list": lambda: self._update_list_payload(raw_id, name, description, description_blocks, todo_mode),
        }[action]
        payload, build_refusal = builder()
        if build_refusal:
            return build_refusal

        if refusal := await self._settings_refusal(metadata, raw_id):
            return refusal

        method = EDIT_OPERATIONS[action]
        try:
            response = await self._api(method, payload)
        except _ListCallFailure as exc:
            return self._slack_refusal(exc, list_id=raw_id, operation=action)

        logger.info("slack list: %s on %s", action, raw_id)
        result: dict[str, Any] = {
            "ok": True,
            "list_id": raw_id,
            "operation": action,
        }
        if action == "create_item":
            record = response.get("record") or response.get("item")
            if isinstance(record, Mapping):
                result["item"] = dict(record)
                result["item_id"] = str(record.get("id") or "").strip()
            if str(parent_item_id or "").strip():
                result["parent_item_id"] = str(parent_item_id).strip()
        elif action == "update_cells":
            result["cells"] = len(payload["cells"])
        elif action == "delete_item":
            result["item_id"] = payload["id"]
            result["deleted"] = True
        elif action == "delete_items":
            result["item_ids"] = list(payload["ids"])
            result["requested"] = len(payload["ids"])
            result["detail"] = (
                "Slack answered this call without refusing it. It documents no"
                " per-item result for a multiple delete, so which of the named"
                " items were removed is not established by this response. Read"
                " the List back to find out."
            )
        return json.dumps(result, ensure_ascii=False)

    def _create_item_payload(
        self,
        list_id: str,
        initial_fields: Any,
        duplicated_item_id: Any,
        parent_item_id: Any,
    ) -> tuple[dict[str, Any], str]:
        """The body of one ``slackLists.items.create``, or the refusal it earns."""
        payload: dict[str, Any] = {"list_id": list_id}
        if initial_fields is not None:
            fields = _object_list(initial_fields)
            if fields is None:
                return {}, _list_refusal_json(
                    "initial_fields_malformed",
                    "initial_fields is a list of cell objects. It was not a list of objects",
                    list_id=list_id,
                )
            rendered, refusal = self._render_cells(fields, list_id)
            if refusal:
                return {}, refusal
            payload["initial_fields"] = rendered
        source = str(duplicated_item_id or "").strip()
        if source:
            payload["duplicated_item_id"] = source
        parent = str(parent_item_id or "").strip()
        if parent:
            payload["parent_item_id"] = parent
        return payload, ""

    def _update_cells_payload(self, list_id: str, cells: Any) -> tuple[dict[str, Any], str]:
        """The body of one ``slackLists.items.update``, or the refusal it earns."""
        rows = _object_list(cells)
        if rows is None:
            return {}, _list_refusal_json(
                "cells_malformed",
                "cells is a list of cell objects, each naming a column and"
                " carrying a value. It was not a list of objects",
                list_id=list_id,
            )
        if not rows:
            return {}, _list_refusal_json(
                "cells_required",
                "cells held nothing, so there is nothing to write and the List is untouched",
                list_id=list_id,
            )
        rendered, refusal = self._render_cells(rows, list_id, require_location=True)
        if refusal:
            return {}, refusal
        return {"list_id": list_id, "cells": rendered}, ""

    @staticmethod
    def _render_cells(
        rows: list[dict[str, Any]], list_id: str, *, require_location: bool = False
    ) -> tuple[list[dict[str, Any]], str]:
        rendered = []
        for row in rows:
            cell, reason = _cell_payload(row)
            if reason:
                return [], _list_refusal_json("cell_malformed", reason, list_id=list_id)
            if require_location:
                for key, detail in (
                    (
                        "column",
                        "every cell names the column it writes. One of them named none",
                    ),
                    (
                        "row",
                        (
                            "every cell names the row it writes, or asks for a new one."
                            " One of them did neither: give it a row_id, or"
                            " row_id_to_create set to true"
                        ),
                    ),
                ):
                    if not str(cell.get(f"{key}_id") or "").strip() and not cell.get(f"{key}_id_to_create"):
                        return [], _list_refusal_json(f"{key}_id_required", detail, list_id=list_id)
            rendered.append(cell)
        return rendered, ""

    def _delete_item_payload(self, list_id: str, item_id: Any) -> tuple[dict[str, Any], str]:
        """The body of one ``slackLists.items.delete``, or the refusal it earns."""
        target = str(item_id or "").strip()
        if not target:
            return {}, _list_refusal_json(
                "item_id_required",
                "no item was named. An item id is taken unchanged from a listing of this List",
                list_id=list_id,
            )
        return {"list_id": list_id, "id": target}, ""

    def _delete_items_payload(self, list_id: str, item_ids: Any) -> tuple[dict[str, Any], str]:
        """The body of one ``slackLists.items.deleteMultiple``, or a refusal."""
        targets = _string_list(item_ids)
        if targets is None:
            return {}, _list_refusal_json(
                "item_ids_malformed",
                "item_ids is a list of item identifiers. It was not a list of strings",
                list_id=list_id,
            )
        if not targets:
            return {}, _list_refusal_json(
                "item_ids_required",
                "item_ids held nothing, so there is nothing to delete and the List is untouched",
                list_id=list_id,
            )
        return {"list_id": list_id, "ids": targets}, ""

    def _update_list_payload(
        self,
        list_id: str,
        name: Any,
        description: Any,
        description_blocks: Any,
        todo_mode: Any,
    ) -> tuple[dict[str, Any], str]:
        """Build a List update; Slack calls its identifier ``id``."""
        blocks, refusal = self._description_refusal(description, description_blocks)
        if refusal:
            return {}, refusal
        payload: dict[str, Any] = {"id": list_id}
        title = str(name or "").strip() if name is not None else ""
        if title:
            payload["name"] = title
        if blocks is not None:
            payload["description_blocks"] = blocks
        if todo_mode is not None:
            if not isinstance(todo_mode, bool):
                return {}, _list_refusal_json(
                    "todo_mode_malformed",
                    "todo_mode is true or false. It was neither",
                    list_id=list_id,
                )
            payload["todo_mode"] = todo_mode
        if len(payload) == 1:
            return {}, _list_refusal_json(
                "nothing_to_update",
                "update_list changes the name, the description or todo mode,"
                " and none was given. It cannot change a column: no Slack"
                " method adds, removes, renames or retypes one",
                list_id=list_id,
            )
        return payload, ""

    def _access_targets(self, chat_ids: Any, user_ids: Any) -> tuple[str, list[str], str]:
        kind, ids, refusal = access_targets(chat_ids, user_ids, reject_dms=False)
        if refusal:
            return "", [], _list_refusal_json(*refusal)
        return "channel_ids" if kind == "chat_ids" else kind, ids, ""

    @checks_slack_ids
    async def share_slack_list(
        self,
        list_id: str,
        access_level: str | None = None,
        chat_ids: Any = None,
        user_ids: Any = None,
        remove: bool = False,
    ) -> str:
        """Set or revoke access to a List for conversations or for people."""
        metadata = self._runtime_metadata()
        if not metadata:
            return self._no_slack_request_refusal()
        raw_id = str(list_id or "").strip()
        refusal = self._list_id_refusal(raw_id)
        if refusal:
            return refusal

        level = str(access_level or "").strip().lower()
        if error := access_level_error(level, remove):
            return _list_refusal_json(*error, list_id=raw_id)

        kind, ids, target_refusal = self._access_targets(chat_ids, user_ids)
        if target_refusal:
            return target_refusal

        if error := owner_error(level, kind, len(ids), resource="List"):
            return _list_refusal_json(*error, list_id=raw_id)

        if refusal := await self._settings_refusal(metadata, raw_id):
            return refusal

        request = SlackAccessRequest(
            method=_LIST_ACCESS_DELETE if remove else _LIST_ACCESS_SET,
            resource_key="list_id",
            resource_id=raw_id,
            kind=kind,
            wire_kind=kind,
            ids=tuple(ids),
            level=level,
            removes=remove,
        )
        return await self._share_access(
            request,
            logger=logger,
            details=_FAILURE_DETAIL,
            refusal=lambda exc: self._slack_refusal(exc, list_id=raw_id, **{kind: list(ids)}),
        )

    @checks_slack_ids
    async def delete_slack_list(self, list_id: str) -> str:
        """Delete one List, permanently, after checking that it is one."""
        metadata = self._runtime_metadata()
        if not metadata:
            return self._no_slack_request_refusal()
        raw_id = str(list_id or "").strip()
        refusal = self._list_id_refusal(raw_id)
        if refusal:
            return refusal

        if refusal := await self._settings_refusal(metadata, raw_id):
            return refusal

        # files.delete accepts any file, so verify its type first.
        try:
            response = await self._call(_FILES_INFO, file=raw_id)
        except _ListCallFailure as exc:
            return self._slack_refusal(exc, list_id=raw_id)
        record = response.get("file")
        if not isinstance(record, Mapping):
            return _list_refusal_json(
                "file_not_found",
                "Slack answered with no record for this id, so there is nothing"
                " this tool can confirm is a List. Nothing was deleted",
                list_id=raw_id,
            )
        filetype = str(record.get("filetype") or "").strip().lower()
        if filetype != "list":
            return _list_refusal_json(
                "not_a_list",
                f"this id names a file of type {filetype or 'unknown'!r} rather"
                f" than a List. Nothing was deleted: the call behind this tool"
                f" deletes any file at all, so it is only sent for a file Slack"
                f" confirms is a List",
                list_id=raw_id,
            )

        try:
            await self._call(_FILES_DELETE, file=raw_id)
        except _ListCallFailure as exc:
            return self._slack_refusal(exc, list_id=raw_id)

        logger.info("slack list: deleted %s", raw_id)
        return json.dumps(
            {
                "ok": True,
                "list_id": raw_id,
                "deleted": True,
                "title": str(record.get("title") or ""),
            },
            ensure_ascii=False,
        )

_COLUMN_ID_RULE = (
    "\nColumn ids are Slack's, not yours. A schema passed to "
    "write_slack_list names each column with a key you chose; Slack keeps "
    "that key and assigns the column an id of its own -- Col followed by "
    "letters and digits -- and every write takes the id. Passing the key "
    "back is refused, and Slack's refusal names no argument. Read the List "
    "with read_slack_list and include_list true, which folds the schema in "
    "beside the rows, and take each column's id from there."
)


_CELL_SHAPES = (
    "\nWhat a cell value looks like. A cell is a column_id and one value "
    "whose key is the column type's own name -- a select column takes "
    "select, a date column takes date -- with one exception: a text column "
    "takes rich_text. Almost every value sits in an array even where the "
    "column holds one thing, and that is the shape most often got wrong. "
    "Slack's documented ones:"
    + "".join(f"\n  {key} -- {shape}." for key, shape in CELL_VALUE_SHAPES.items())
    + "\nTypes Slack shows no cell for: "
    + ", ".join(UNDOCUMENTED_CELL_TYPES)
    + ". Slack says every column type can be written and publishes a key "
    "for none of these, so none is guessed at here. Write what the type "
    "name suggests and read the refusal if it is wrong."
    "\nNothing is reshaped on the way out. A scalar is not wrapped in an "
    "array and no key is renamed, because a tool that accepted what Slack "
    "refuses would teach the wrong shape for next time."
)


_TEXT_COVERAGE = (
    "\nWhat text carries. Bold, italic, strikethrough, inline code, links, "
    "bullet and numbered lists, block quotes, fenced code blocks, emoji "
    "shortcodes, and Slack's own <@U…> and <#C…> tokens."
    "\nWhat text does not carry, so that nothing is lost without warning. A "
    "table arrives as its pipe characters -- rich text has no table of any "
    "kind. A heading arrives bold with its level gone, so # and ###### look "
    "alike. An image arrives as a link to itself. Horizontal rules arrive as "
    "characters, as does a second level of block quote and an indented (rather "
    "than fenced) code block. A list item is the one line it was written on. A "
    "checkbox arrives as the characters [ ]. A mention written as a name -- "
    "@alice, #general -- stays text, because turning it into a real mention "
    "would need a directory this does not consult; write <@U01ABCDEF> for a "
    "real one. A link needs a scheme, so [here](/docs) stays characters. "
    "Styling inside part of a link label is dropped unless it covers the whole "
    "label. If any of that matters, build the payload yourself and pass it as "
    "rich_text instead."
)


def _read_tool_definition() -> dict[str, Any]:
    """Build the read tool card."""
    return {
        "name": "read_slack_list",
        "description": (
            "Read a Slack List page, one item, or an export. Nested items are "
            "included. Pass item_id for one item, or export for the whole List."
            "\nA single item includes the List schema, views and limits. "
            "Slack has no method that returns a List on its own. "
            "Pass include_list true with a page to obtain the schema. "
            "Use Slack-generated column ids for edits."
            "\nThere is no way to search or sort a page. Read every page to "
            "filter rows locally. Do not look for a query argument; there is none. "
            "Use next_cursor as cursor to continue. Archived rows are separate."
            "\nExport defaults to csv. Use json for nested rows, threads, or "
            "attachments. If an export is pending, repeat its export_job_id "
            "with the same options. A finished export returns full content within the operator's "
            "lists_export_max_chars and lists_export_max_bytes settings. Oversized content is refused "
            "without a partial document; retry the same export_job_id after raising the setting. "
            "Its download_url needs this app's token. "
            "Slack Lists require a paid plan."
        ),
        "input_params": {
            "type": "object",
            "properties": {
                "list_id": {
                    "type": "string",
                    "description": (
                        "The List to read, such as F1234ABCD. Copy it verbatim from a result that reported one."
                    ),
                },
                "item_id": {
                    "type": "string",
                    "description": (
                        "Read this one row instead of a page. The List's own "
                        "schema, views and limits come back with it."
                    ),
                },
                "include_is_subscribed": {
                    "type": "boolean",
                    "default": False,
                    "description": ("Also report whether this app is subscribed to the row. Needs item_id."),
                },
                "archived": {
                    "type": "boolean",
                    "default": False,
                    "description": ("Read the archived rows rather than the live ones."),
                },
                "include_list": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Fold the List's own details -- its title, column "
                        "schema and total row count -- in beside the rows. "
                        "The schema is where each column's generated id "
                        "lives, which is what a write takes."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "How many rows to return in this page. At most "
                        f"{MAX_ITEMS_PER_PAGE}, which is also the default."
                    ),
                },
                "cursor": {
                    "type": "string",
                    "description": (
                        "Continue a listing from where it stopped. Use the next_cursor a previous page reported."
                    ),
                },
                "export": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Export the whole List as a file instead of reading rows. Cannot be combined with item_id."
                    ),
                },
                "export_format": {
                    "type": "string",
                    "enum": list(EXPORT_FORMATS),
                    "description": (
                        "The export's format. csv is the default; json is the "
                        "complete, hierarchical export and the only format "
                        "that honours include_threads and include_attachments."
                    ),
                },
                "export_job_id": {
                    "type": "string",
                    "description": (
                        "Ask after an export already started rather than "
                        "starting another. Pass the same export_format, "
                        "include_threads and include_attachments it was "
                        "started with; Slack requires them to match."
                    ),
                },
                "include_archived": {
                    "type": "boolean",
                    "default": False,
                    "description": ("Include archived rows in the export. Export only."),
                },
                "include_threads": {
                    "type": "boolean",
                    "default": False,
                    "description": ("Include each row's conversation thread in the export. json format only."),
                },
                "include_attachments": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Include file attachment details and access paths in the export. json format only."
                    ),
                },
            },
            "required": ["list_id"],
        },
    }


def _write_tool_definition() -> dict[str, Any]:
    """Build the write tool card."""
    return {
        "name": "write_slack_list",
        "description": (
            "Create a Slack List of typed columns and rows. Its columns "
            "cannot be changed later. No Slack method adds a column, removes "
            "one, renames one, or changes its type. Without schema, Slack "
            "creates one text column."
            "\nA schema key is not what writes it. Slack assigns a column id. "
            "Read the List with include_list true to get those ids before "
            "editing rows."
            "\nPass copy_from_list_id to copy another List's columns. "
            "include_copied_list_records also copies rows. A copy and a "
            "schema cannot be combined. todo_mode adds Slack's Completed, "
            "Assignee, and Due Date columns."
            "\nUse description for Markdown or description_blocks for Block Kit. "
            "The new List is visible only to this app until shared. Creation "
            "notifies nobody. Lists require a paid plan."
        ),
        "input_params": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The List's name.",
                },
                "description": {
                    "type": "string",
                    "description": ("What the List is for, in Markdown. Cannot be given with description_blocks."),
                },
                "description_blocks": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "The description as Block Kit, supplied verbatim "
                        "instead of description. Use it when the Markdown "
                        "rendering is not what is wanted."
                    ),
                },
                "schema": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {
                                "type": "string",
                                "description": (
                                    "The column's own key, chosen here. It "
                                    "names the column in this schema and "
                                    "nowhere else: writing a cell later takes "
                                    "the id Slack generates beside it, not "
                                    "this."
                                ),
                            },
                            "name": {
                                "type": "string",
                                "description": ("What the column is called on screen."),
                            },
                            "type": {
                                "type": "string",
                                "description": (
                                    "The column's type. Slack's own example "
                                    "writes out: "
                                    + ", ".join(COLUMN_TYPES[:20])
                                    + ". todo_mode adds "
                                    + ", ".join(COLUMN_TYPES[20:])
                                    + ". Those are what Slack documents rather "
                                    "than a closed set this tool enforces."
                                ),
                            },
                            "is_primary_column": {
                                "type": "boolean",
                                "description": ("Set on the one column that titles each row."),
                            },
                            "options": {
                                "type": "object",
                                "description": (
                                    "The column's own settings, whose shape "
                                    "depends on its type -- a select's "
                                    "choices, a number's precision, a rating's "
                                    "emoji and maximum."
                                ),
                            },
                        },
                    },
                    "description": (
                        "The columns, in Slack's own column-definition shape. "
                        "Omit it for a single text column. These columns are "
                        "permanent: nothing can change them afterwards."
                    ),
                },
                "copy_from_list_id": {
                    "type": "string",
                    "description": (
                        "Create this List as a copy of another one, taking its columns. Cannot be given with schema."
                    ),
                },
                "include_copied_list_records": {
                    "type": "boolean",
                    "default": False,
                    "description": ("Copy the other List's rows as well as its columns. Needs copy_from_list_id."),
                },
                "todo_mode": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Add Slack's task-tracking columns -- completed, "
                        "assignee, due date -- which do not count against the "
                        "column limit."
                    ),
                },
            },
            "required": ["name"],
        },
    }


_CELL_PROPERTIES = {
    "column_id": {
        "type": "string",
        "description": (
            "Use the id Slack generated for it, not the key from the schema. "
            "Read the List with include_list true to find the id."
        ),
    },
    "text": {
        "type": "string",
        "description": "Markdown for a text cell. Cannot be given with rich_text.",
    },
    "rich_text": {
        "type": "array",
        "items": {"type": "object"},
        "description": "A rich_text Block Kit value instead of text.",
    },
}


def _edit_tool_definition() -> dict[str, Any]:
    """Build the edit tool card."""
    return {
        "name": "edit_slack_list",
        "description": (
            "Change a Slack List using operation: create_item, update_cells, "
            "delete_item, delete_items, or update_list. It cannot change a "
            "column. update_list changes only name, description, or todo_mode."
            "\nupdate_cells writes a batch across rows and columns. Each cell "
            "names row_id and column_id. row_id_to_create makes a new row."
            + _COLUMN_ID_RULE
            + _CELL_SHAPES
            + "\nFor text cells, plain text is not accepted in a request. "
            "Use text for Markdown conversion or rich_text for Block Kit. "
            "Passing both is refused." + _TEXT_COVERAGE + "\ncreate_item uses initial_fields, duplicated_item_id, or "
            "parent_item_id for a subtask. Slack reports no per-row result "
            "for delete_items. Read the List to confirm which rows remain. "
            "Slack also calls rows items or records."
        ),
        "input_params": {
            "type": "object",
            "properties": {
                "list_id": {
                    "type": "string",
                    "description": ("The List to change, such as F1234ABCD."),
                },
                "operation": {
                    "type": "string",
                    "enum": sorted(EDIT_OPERATIONS),
                    "description": "What to do.",
                },
                "cells": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "row_id": {
                                "type": "string",
                                "description": "The row to write into.",
                            },
                            "row_id_to_create": {
                                "type": "boolean",
                                "description": (
                                    "Create a new row for this cell instead of writing into an existing one."
                                ),
                            },
                            **_CELL_PROPERTIES,
                        },
                        "description": (
                            "One cell. Besides the keys above it carries one "
                            "value whose key is its column's type -- select, "
                            "user, date, number, checkbox, email, phone and "
                            "so on. Every documented value but checkbox is "
                            "an array, including the ones whose column holds "
                            "a single thing; the tool's own description "
                            "lists the shape each key takes."
                        ),
                    },
                    "description": ("For update_cells: the cells to write, across any number of rows and columns."),
                },
                "initial_fields": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": _CELL_PROPERTIES,
                        "description": (
                            "One cell of the new row. Besides column_id it "
                            "carries one value whose key is its column's "
                            "type, and every documented value but checkbox "
                            "is an array; the tool's own description lists "
                            "the shape each key takes. A row id is neither "
                            "given nor needed -- the row is being made."
                        ),
                    },
                    "description": (
                        "For create_item: the new row's cells, in the same "
                        "shape the cells argument takes minus the row keys."
                    ),
                },
                "duplicated_item_id": {
                    "type": "string",
                    "description": ("For create_item: copy this existing row rather than making an empty one."),
                },
                "parent_item_id": {
                    "type": "string",
                    "description": ("For create_item: make the new row a subtask of this row."),
                },
                "item_id": {
                    "type": "string",
                    "description": "For delete_item: the row to remove.",
                },
                "item_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": ("For delete_items: the rows to remove. Slack reports no per-row result for this."),
                },
                "name": {
                    "type": "string",
                    "description": "For update_list: the List's new name.",
                },
                "description": {
                    "type": "string",
                    "description": (
                        "For update_list: the List's new description, in "
                        "Markdown. Cannot be given with description_blocks."
                    ),
                },
                "description_blocks": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "For update_list: the new description as Block Kit, supplied verbatim instead of description."
                    ),
                },
                "todo_mode": {
                    "type": "boolean",
                    "description": ("For update_list: turn Slack's task-tracking columns on or off."),
                },
            },
            "required": ["list_id", "operation"],
        },
    }


def _share_tool_definition() -> dict[str, Any]:
    """Build the share tool card."""
    return {
        "name": "share_slack_list",
        "description": (
            "Set who may read or write a Slack List, or take that access away."
            + ACCESS_TARGETS_PROSE
            + ACCESS_PARTIAL_PROSE
            + "\nSharing a List with a conversation makes it reachable by "
            "everybody in that conversation. It notifies nobody; to tell "
            "people it exists, say so."
        ),
        "input_params": {
            "type": "object",
            "properties": {
                "list_id": {
                    "type": "string",
                    "description": ("The List whose access is being set, such as F1234ABCD."),
                },
                **ACCESS_PARAMS,
                "user_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": ("The people to set access for. Cannot be given together with chat_ids."),
                },
            },
            "required": ["list_id"],
        },
    }


def _delete_tool_definition() -> dict[str, Any]:
    """Build the delete tool card."""
    return {
        "name": "delete_slack_list",
        "description": (
            "Delete a Slack List."
            "\nIt cannot be undone. Slack keeps no copy and has no undelete: "
            "every row the List held is gone, along with every link anybody "
            "has to it, and nobody is told. Delete a List when somebody asked "
            "for it to be deleted."
            "\nOnly a List. The id is checked against Slack before anything is "
            "deleted, and an id naming anything else -- a canvas, an uploaded "
            "file, an image -- is refused and nothing happens to it."
            "\nDeleting files can be switched off for a whole workspace, in "
            "which case this fails and says so, and nothing but an "
            "administrator change will alter that."
        ),
        "input_params": {
            "type": "object",
            "properties": {
                "list_id": {
                    "type": "string",
                    "description": ("The List to delete, such as F1234ABCD."),
                },
            },
            "required": ["list_id"],
        },
    }
