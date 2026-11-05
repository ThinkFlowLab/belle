"""Build a daily report from a Slack list.

Use Assignee, then Submitted by, to group rows. Use Last Updated, then Date
submitted, for recency. Date cells use an inclusive calendar-day cutoff.
Timestamps use an exact cutoff. Monday reports cover 72 hours. The bot needs
``lists:read``, ``users:read``, ``SLACK_BOT_TOKEN``, and ``SLACK_LIST_ID``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, TypeVar

import requests
from requests.adapters import HTTPAdapter

API_BASE = "https://slack.com/api"

DEFAULT_STATUS_COLUMN = "Status"
DEFAULT_ASSIGNEE_COLUMN = "Assignee"
DEFAULT_SUBMITTER_COLUMN = "Submitted by"
DEFAULT_WIP_STATUS = "WIP"
DEFAULT_DONE_STATUS = "Done"
DEFAULT_UPDATED_COLUMN = "Last Updated"
DEFAULT_SUBMITTED_COLUMN = "Date submitted"

UNASSIGNED = "Unassigned"

DEFAULT_WINDOW_HOURS = 24
WEEKEND_CATCHUP_WINDOW_HOURS = 72
MONDAY = 0  # datetime.weekday()

TODO_SECTION, WIP_SECTION, DONE_SECTION = ":todo:", ":wip:", ":done:"

MAX_CONCURRENCY = 8
PAGE_SIZE = 100
MAX_PAGES = 100
MAX_RETRIES = 3
REQUEST_TIMEOUT_SECONDS = 30.0
RETRY_BACKOFF_SECONDS = (1.0, 3.0, 8.0)
MAX_RETRY_AFTER_SECONDS = 30.0

T = TypeVar("T")
R = TypeVar("R")


class SlackError(RuntimeError):
    """A Slack API failure whose message never carries the token."""


@dataclass(frozen=True)
class Moment:
    """A timestamp or calendar day. ``date_only`` selects cutoff granularity."""

    when: datetime
    date_only: bool


@dataclass
class Column:
    key: str
    name: str
    type: str
    is_primary: bool = False
    choices: dict[str, str] = field(default_factory=dict)


@dataclass
class Item:
    id: str
    title: str
    owners: list[str]
    status: str | None
    updated: Moment | None


@dataclass
class MemberReport:
    id: str
    name: str
    started: list[str] = field(default_factory=list)
    wip: list[str] = field(default_factory=list)
    done: list[str] = field(default_factory=list)


def _new_session() -> requests.Session:
    """Use one connection per worker. ``SlackClient.call`` handles retries."""
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})
    session.mount(
        "https://",
        HTTPAdapter(pool_connections=1, pool_maxsize=1, max_retries=0),
    )
    return session


class SlackClient:
    """Read Slack with one session per worker. Session thread safety is not documented."""

    def __init__(
        self,
        token: str,
        *,
        pool: ThreadPoolExecutor,
        session_factory: Callable[[], requests.Session] = _new_session,
    ) -> None:
        self._token = token
        self._pool = pool
        self._session_factory = session_factory
        self._local = threading.local()
        self._lock = threading.Lock()
        self._sessions: list[requests.Session] = []

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._local.session = self._session_factory()
            session.headers["Authorization"] = f"Bearer {self._token}"
            with self._lock:
                self._sessions.append(session)
        return session

    def close(self) -> None:
        with self._lock:
            sessions, self._sessions = self._sessions, []
        for session in sessions:
            session.close()

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        """POST form data with explicit booleans. Retry rate limits and 5xx."""
        body = {
            key: ("true" if value is True else "false" if value is False else value)
            for key, value in params.items()
            if value is not None
        }
        last_error = "unknown error"
        for attempt in range(MAX_RETRIES + 1):
            delay = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
            try:
                response = self._session().post(
                    f"{API_BASE}/{method}",
                    data=body,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                last_error = self._redact(f"{type(exc).__name__}: {exc}")
            else:
                if response.status_code == 429:
                    delay = _retry_after(response, delay)
                    last_error = "HTTP 429"
                elif response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}"
                elif response.status_code != 200:
                    # 401/403/404 will not improve on a retry.
                    raise SlackError(f"{method}: HTTP {response.status_code}")
                else:
                    return self._unwrap(method, response)

            if attempt < MAX_RETRIES:
                time.sleep(delay)

        raise SlackError(f"{method}: {last_error} after {MAX_RETRIES} retries")

    @staticmethod
    def _unwrap(method: str, response: requests.Response) -> dict[str, Any]:
        """Check Slack's ``ok`` field even when HTTP status is 200."""
        try:
            payload = response.json()
        except ValueError:
            raise SlackError(f"{method}: response was not JSON") from None
        if not isinstance(payload, dict):
            raise SlackError(f"{method}: response was not an object")
        if not payload.get("ok"):
            error = payload.get("error") or "unknown error"
            needed = payload.get("needed")
            if needed:
                error = f"{error} (needs scope: {needed})"
            raise SlackError(f"{method}: {error}")
        return payload

    def map(self, fn: Callable[[T], R], items: Iterable[T]) -> list[R]:
        """Map from the main thread to avoid workers waiting on their own pool."""
        items = list(items)
        if len(items) <= 1:
            return [fn(item) for item in items]
        return list(self._pool.map(fn, items))


def _retry_after(response: requests.Response, fallback: float) -> float:
    """Slack's own back-off hint, clamped so a bad header cannot stall a run."""
    raw = response.headers.get("Retry-After")
    try:
        seconds = float(raw) if raw is not None else fallback
    except ValueError:
        return fallback
    return max(0.0, min(seconds, MAX_RETRY_AFTER_SECONDS))


def default_window_hours(now: datetime | None = None) -> int:
    """Use 72 hours on local Monday and 24 hours on other local days."""
    now = now or datetime.now().astimezone()
    if now.weekday() == MONDAY:
        return WEEKEND_CATCHUP_WINDOW_HOURS
    return DEFAULT_WINDOW_HOURS


def _window_phrase(hours: int) -> str:
    """Format hours for the report heading."""
    if hours >= 48 and hours % 24 == 0:
        return f"{hours // 24} days"
    return f"{hours}h"


def _parse_schema(raw: Any) -> list[Column]:
    """Turn the list's column schema into something addressable by name."""
    columns: list[Column] = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key") or entry.get("id")
        if not key:
            continue
        options = entry.get("options") or {}
        choices = {
            choice["value"]: choice.get("label") or choice["value"]
            for choice in options.get("choices") or []
            if isinstance(choice, dict) and choice.get("value")
        }
        columns.append(
            Column(
                key=str(key),
                name=entry.get("name") or str(key),
                type=entry.get("type") or "",
                is_primary=bool(entry.get("is_primary_column")),
                choices=choices,
            )
        )
    return columns


def _find_column(columns: list[Column], name: str, *, label: str) -> Column:
    match = _maybe_column(columns, name)
    if match is None:
        known = ", ".join(sorted(column.name for column in columns)) or "none"
        raise SlackError(f"the list has no {label} column named {name!r}. Columns: {known}")
    return match


def _maybe_column(columns: list[Column], name: str) -> Column | None:
    """Match a column by display name, then API key, ignoring case."""
    wanted = name.strip().casefold()
    for attribute in ("name", "key"):
        for column in columns:
            if getattr(column, attribute).casefold() == wanted:
                return column
    return None


def _cells(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index row cells by both column key and ID."""
    cells: dict[str, dict[str, Any]] = {}
    for cell in item.get("fields") or []:
        if not isinstance(cell, dict):
            continue
        for identifier in (cell.get("key"), cell.get("column_id")):
            if identifier:
                cells.setdefault(str(identifier), cell)
    return cells


def _typed(cell: dict[str, Any] | None, kind: str) -> list[Any]:
    """Read nonempty values from a typed cell."""
    if not cell:
        return []
    raw = cell.get(kind)
    if raw is None:
        return []
    values = raw if isinstance(raw, list) else [raw]
    return [value for value in values if value not in (None, "")]


def _plain_text(cell: dict[str, Any] | None) -> str:
    """Read text from a plain or nested rich-text cell."""
    if not cell:
        return ""
    for key in ("text", "value"):
        value = cell.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    parts: list[str] = []
    _flatten_rich_text(cell.get("rich_text"), parts)
    return "".join(parts).strip()


def _flatten_rich_text(node: Any, into: list[str]) -> None:
    if isinstance(node, list):
        for child in node:
            _flatten_rich_text(child, into)
    elif isinstance(node, dict):
        text = node.get("text")
        if isinstance(text, str):
            into.append(text)
        _flatten_rich_text(node.get("elements"), into)


def _parse_moment(cell: dict[str, Any] | None) -> Moment | None:
    """Read an exact timestamp or a calendar day."""
    for value in _typed(cell, "timestamp"):
        try:
            seconds = float(value)
            if seconds < 0:
                continue
        except (TypeError, ValueError):
            continue
        return Moment(datetime.fromtimestamp(seconds, UTC), date_only=False)

    for value in _typed(cell, "date"):
        parsed = _parse_day(value)
        if parsed is not None:
            return Moment(datetime(parsed.year, parsed.month, parsed.day, tzinfo=UTC), date_only=True)
    return None


def _parse_day(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _is_recent(moment: Moment | None, *, cutoff: datetime, cutoff_day: date) -> bool:
    """Compare dates by inclusive day and timestamps by exact time."""
    if moment is None:
        return False
    if moment.date_only:
        return moment.when.date() >= cutoff_day
    return moment.when >= cutoff


def _fetch_items(slack: SlackClient, token: str, list_id: str) -> tuple[list[Column], list[dict]]:
    """Every row in the list, with the column schema the first page carries."""
    columns: list[Column] | None = None
    items: list[dict[str, Any]] = []
    cursor: str | None = None

    for _ in range(MAX_PAGES):
        payload = slack.call(
            "slackLists.items.list",
            token=token,
            list_id=list_id,
            limit=PAGE_SIZE,
            cursor=cursor,
            # Only the first page needs to carry the schema.
            include_list=columns is None,
        )
        if columns is None:
            parent = payload.get("list", {})
            columns = _parse_schema(parent.get("list_metadata", {}).get("schema"))
            if not columns:
                raise SlackError(
                    f"list {list_id!r} returned no column schema; check the id "
                    f"names a Slack list and the bot is a member of its channel"
                )
        items.extend(item for item in payload.get("items") or [] if isinstance(item, dict))
        cursor = (payload.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            return columns, items

    raise SlackError(f"list {list_id!r} did not end after {MAX_PAGES} pages")


def _resolve_status(column: Column, wanted: str, *, label: str) -> str:
    """Match a status by value or label. Keep it if the schema lists no choices."""
    if not column.choices:
        return wanted
    target = wanted.strip().casefold()
    for value, choice_label in column.choices.items():
        if target in (value.casefold(), choice_label.casefold()):
            return value
    known = ", ".join(sorted(column.choices.values())) or "none"
    raise SlackError(f"column {column.name!r} has no {label} choice named {wanted!r}. Choices: {known}")


def _read_items(
    items: list[dict[str, Any]],
    *,
    columns: list[Column],
    title_column: Column,
    status_column: Column,
    owner_columns: list[Column],
    date_columns: list[Column],
) -> list[Item]:
    """Read rows, using the first nonempty owner and date columns."""
    parsed: list[Item] = []
    for raw in items:
        if raw.get("archived"):
            continue
        cells = _cells(raw)
        statuses = _typed(cells.get(status_column.key), "select")
        owners = next(
            (found for column in owner_columns if (found := _typed(cells.get(column.key), "user"))),
            [],
        )
        moment = next(
            (found for column in date_columns if (found := _parse_moment(cells.get(column.key))) is not None),
            None,
        )
        parsed.append(
            Item(
                id=str(raw.get("id") or ""),
                title=_plain_text(cells.get(title_column.key)) or "(untitled)",
                owners=[str(user) for user in owners],
                status=str(statuses[0]) if statuses else None,
                updated=moment,
            )
        )
    return parsed


def _display_names(slack: SlackClient, user_ids: list[str]) -> dict[str, str]:
    """Resolve user IDs, retaining IDs when lookup fails."""

    def lookup(user_id: str) -> str:
        try:
            user = slack.call("users.info", user=user_id).get("user") or {}
        except SlackError:
            return user_id
        profile = user.get("profile") or {}
        for candidate in (
            profile.get("display_name"),
            profile.get("real_name"),
            user.get("real_name"),
            user.get("name"),
        ):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return user_id

    return dict(zip(user_ids, slack.map(lookup, user_ids), strict=True))


def _member_reports(
    items: list[Item],
    *,
    names: dict[str, str],
    wip_status: str,
    done_status: str,
    cutoff: datetime,
    cutoff_day: date,
) -> list[MemberReport]:
    """Group rows by owner. Put ownerless rows under ``UNASSIGNED``."""
    reports: dict[str, MemberReport] = {}

    def report_for(user_id: str) -> MemberReport:
        if user_id not in reports:
            reports[user_id] = MemberReport(id=user_id, name=names.get(user_id, user_id))
        return reports[user_id]

    for item in items:
        if item.status not in (wip_status, done_status):
            continue
        recent = _is_recent(item.updated, cutoff=cutoff, cutoff_day=cutoff_day)
        if item.status == done_status and not recent:
            continue
        for owner in item.owners or [UNASSIGNED]:
            report = report_for(owner)
            if item.status == wip_status:
                if recent:
                    report.started.append(item.title)
                else:
                    report.wip.append(item.title)
            else:
                report.done.append(item.title)

    for report in reports.values():
        report.started.sort()
        report.wip.sort()
        report.done.sort()

    return sorted(
        reports.values(),
        key=lambda report: (report.id == UNASSIGNED, report.name.casefold()),
    )


def build_daily_update(
    *,
    list_id: str | None = None,
    status_column_name: str = DEFAULT_STATUS_COLUMN,
    assignee_column_name: str = DEFAULT_ASSIGNEE_COLUMN,
    submitter_column_name: str = DEFAULT_SUBMITTER_COLUMN,
    updated_column_name: str = DEFAULT_UPDATED_COLUMN,
    submitted_column_name: str = DEFAULT_SUBMITTED_COLUMN,
    wip_status: str = DEFAULT_WIP_STATUS,
    done_status: str = DEFAULT_DONE_STATUS,
    window_hours: int | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Collect report data. Use a 72-hour window on Mondays by default."""
    tok = token or os.environ.get("SLACK_BOT_TOKEN", "")
    target = list_id or os.environ.get("SLACK_LIST_ID", "")
    missing = [name for name, value in (("SLACK_BOT_TOKEN", tok), ("SLACK_LIST_ID", target)) if not value]
    if missing:
        raise SlackError(f"missing configuration: {', '.join(missing)}")

    now = datetime.now(UTC)
    if window_hours is None:
        window_hours = default_window_hours(now.astimezone())
    cutoff = now - timedelta(hours=window_hours)
    cutoff_day = (now.astimezone() - timedelta(hours=window_hours)).date()

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as pool:
        slack = SlackClient(tok, pool=pool)
        try:
            columns, raw_items = _fetch_items(slack, tok, target)

            status_column = _find_column(columns, status_column_name, label="status")
            owner_columns = [
                column
                for column in (
                    _find_column(columns, assignee_column_name, label="assignee"),
                    _maybe_column(columns, submitter_column_name),
                )
                if column is not None
            ]
            date_columns = [
                column
                for column in (
                    _maybe_column(columns, updated_column_name),
                    _maybe_column(columns, submitted_column_name),
                )
                if column is not None
            ]
            if not date_columns:
                known = ", ".join(sorted(column.name for column in columns)) or "none"
                raise SlackError(
                    f"the list has neither a {updated_column_name!r} nor a "
                    f"{submitted_column_name!r} column, so nothing can be dated; "
                    f"columns present: {known}"
                )
            title_column = next((column for column in columns if column.is_primary), columns[0])

            wip_value = _resolve_status(status_column, wip_status, label="WIP")
            done_value = _resolve_status(status_column, done_status, label="Done")

            items = _read_items(
                raw_items,
                columns=columns,
                title_column=title_column,
                status_column=status_column,
                owner_columns=owner_columns,
                date_columns=date_columns,
            )
            user_ids = sorted({user for item in items for user in item.owners})
            names = _display_names(slack, user_ids)
            reports = _member_reports(
                items,
                names=names,
                wip_status=wip_value,
                done_status=done_value,
                cutoff=cutoff,
                cutoff_day=cutoff_day,
            )
        finally:
            slack.close()

    return {
        "list": {"id": target},
        "generated_at": now.isoformat(),
        "window_hours": window_hours,
        "owned_by": [column.name for column in owner_columns],
        "dated_by": [column.name for column in date_columns],
        "members": [
            {
                "id": report.id,
                "name": report.name,
                "started": report.started,
                "wip": report.wip,
                "done": report.done,
            }
            for report in reports
        ],
    }


def block_kit(update: dict[str, Any], *, skip_idle: bool = False) -> dict[str, Any]:
    """Render the collected update in the layout the skill's example shows."""
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*Daily Update*"}}]

    for member in update["members"]:
        started, wip, done = member["started"], member["wip"], member["done"]
        if skip_idle and not (started or wip or done):
            continue

        lines: list[str] = []

        block = {
            "type": "container",
            "block_id": f"{member['name'].lower()}_container_collapsible",
            "title": {"type": "plain_text", "text": member["name"]},
            "is_collapsible": True,
            "default_collapsed": True,
            "child_blocks": [{"type": "section", "text": {"type": "mrkdwn"}}],
        }

        if started:
            lines.append(f"{TODO_SECTION} *Started*")
            lines.extend(f" • {name}" for name in started)

        if wip:
            lines.append(f"{WIP_SECTION} *WIP*")
            lines.extend(f" • {name}" for name in wip)

        if done:
            lines.append(f"{DONE_SECTION} *Done*")
            lines.extend(f" • {name}" for name in done)

        block["child_blocks"][0]["text"]["text"] = "\n".join(lines)

        blocks.append(block)

    return {"blocks": blocks}


def format_daily_update(update: dict[str, Any], *, skip_idle: bool = False) -> str:
    """Render the collected update in the layout the skill's example shows."""
    blocks: list[str] = []
    for member in update["members"]:
        started, wip, done = member["started"], member["wip"], member["done"]
        if skip_idle and not (started or wip or done):
            continue

        lines = [f"**{member['name']}**", ""]

        if started:
            lines.append(f"{TODO_SECTION} **Started**")
            lines.extend(f"  - {name}" for name in started)

        if wip:
            lines.append(f"{WIP_SECTION} **WIP**")
            lines.extend(f"  - {name}" for name in wip)

        if done:
            lines.append(f"{DONE_SECTION} **Done**")
            lines.extend(f"  - {name}" for name in done)

        blocks.append("\n".join(lines))

    if not blocks:
        window = _window_phrase(update["window_hours"])
        return f"No activity on the list in the last {window}."
    return "\n\n".join(blocks)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--list-id",
        default=None,
        help="Slack list ID (an F-prefixed id); defaults to $SLACK_LIST_ID",
    )
    parser.add_argument(
        "--status-column",
        default=DEFAULT_STATUS_COLUMN,
        help=f"select column holding a task's state (default: {DEFAULT_STATUS_COLUMN!r})",
    )
    parser.add_argument(
        "--assignee-column",
        default=DEFAULT_ASSIGNEE_COLUMN,
        help=f"user column holding who owns a task (default: {DEFAULT_ASSIGNEE_COLUMN!r})",
    )
    parser.add_argument(
        "--submitter-column",
        default=DEFAULT_SUBMITTER_COLUMN,
        help=f"user column used when the assignee one is empty (default: {DEFAULT_SUBMITTER_COLUMN!r})",
    )
    parser.add_argument(
        "--wip-status",
        default=DEFAULT_WIP_STATUS,
        help=f"status counted as in progress (default: {DEFAULT_WIP_STATUS!r})",
    )
    parser.add_argument(
        "--done-status",
        default=DEFAULT_DONE_STATUS,
        help=f"status counted as finished (default: {DEFAULT_DONE_STATUS!r})",
    )
    parser.add_argument(
        "--updated-column",
        default=DEFAULT_UPDATED_COLUMN,
        help=f"date column deciding recency (default: {DEFAULT_UPDATED_COLUMN!r})",
    )
    parser.add_argument(
        "--submitted-column",
        default=DEFAULT_SUBMITTED_COLUMN,
        help=f"date column used when the updated one is empty (default: {DEFAULT_SUBMITTED_COLUMN!r})",
    )
    parser.add_argument(
        "--window-hours",
        type=int,
        default=None,
        help=f"how far back Started and Done look; defaults to "
        f"{DEFAULT_WINDOW_HOURS}h, or {WEEKEND_CATCHUP_WINDOW_HOURS}h on a "
        f"Monday so the weekend is covered",
    )
    parser.add_argument("--json", action="store_true", help="emit the raw data instead")
    parser.add_argument(
        "--skip-idle",
        action="store_true",
        help="omit members with nothing in any section",
    )
    parser.add_argument("--block-kit", action="store_true", help="render result as a Slack Block element")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        update = build_daily_update(
            list_id=args.list_id,
            status_column_name=args.status_column,
            assignee_column_name=args.assignee_column,
            submitter_column_name=args.submitter_column,
            updated_column_name=args.updated_column,
            submitted_column_name=args.submitted_column,
            wip_status=args.wip_status,
            done_status=args.done_status,
            window_hours=args.window_hours,
        )
    except SlackError as exc:
        print(f"slack-daily-update: {exc}", file=sys.stderr)
        return 1

    if args.block_kit:
        print(json.dumps(block_kit(update, skip_idle=args.skip_idle)))
    elif args.json:
        print(json.dumps(update, indent=2, ensure_ascii=False))
    else:
        print(format_daily_update(update, skip_idle=args.skip_idle))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
