"""Slack status cards and placeholder records for scheduled runs."""

import logging
import time
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Any

from .activity import (
    _CARD_STATUS_COMPLETE,
    _CARD_STATUS_ERROR,
    _CARD_STATUS_IN_PROGRESS,
    _MAX_CARD_DETAILS_LENGTH,
    _MAX_CARD_TITLE_LENGTH,
    ActivityCardRenderer,
)
from .text import clamp

logger = logging.getLogger(__name__)

MAX_CRON_RECORDS = 32
CRON_RECORD_TIMEOUT_SECONDS = 3600.0
_TERMINAL_STATUS = {"succeeded": _CARD_STATUS_COMPLETE, "failed": _CARD_STATUS_ERROR}


@dataclass
class CronRecord:
    run_id: str
    channel_id: str
    message_ts: str
    touched_at: float = field(default_factory=time.monotonic)


@dataclass
class CronPush:
    run_id: str
    channel_id: str
    is_placeholder: bool
    card: dict[str, Any] | None
    anchor_ts: str = ""
    body_below_card: bool = False


def cron_payload(payload: Any) -> dict[str, Any] | None:
    """Recognize a scheduled push with a usable correlation key."""
    if not isinstance(payload, dict):
        return None
    cron = payload.get("cron")
    if not isinstance(cron, dict) or not str(cron.get("run_id") or "").strip():
        return None
    return cron


def cron_task_card(cron: Mapping[str, Any], *, card_status: str, body: str) -> dict[str, Any]:
    run_id = str(cron.get("run_id") or "").strip()
    title = str(cron.get("job_name") or "").strip() or "Scheduled job"
    details: list[str] = []
    if card_status == _CARD_STATUS_ERROR:
        reason = (body or "").strip().splitlines()
        if reason:
            details.append(reason[0].strip())
    scheduled = str(cron.get("push_at") or "").strip()
    if scheduled:
        details.append(f"Scheduled for {scheduled}")
    detail_text = clamp("\n".join(details), _MAX_CARD_DETAILS_LENGTH)
    return ActivityCardRenderer._task_card(
        f"cron-{run_id}",
        clamp(title, _MAX_CARD_TITLE_LENGTH),
        card_status,
        details=ActivityCardRenderer._rich_text_lines([[{"type": "text", "text": detail_text}]])
        if detail_text else None,
    )


def cron_status_record(
    payload: Any,
    channel_id: str,
    content: str,
    records: MutableMapping[tuple[str, str], CronRecord],
) -> CronPush | None:
    """Supersede a placeholder and choose the visible status card."""
    cron = cron_payload(payload)
    if cron is None:
        return None
    run_id = str(cron.get("run_id") or "").strip()
    is_placeholder = bool(cron.get("is_placeholder"))
    status = str(cron.get("status") or "").strip().lower()
    if is_placeholder:
        card_status = _CARD_STATUS_IN_PROGRESS
        anchor_ts = ""
    else:
        card_status = _TERMINAL_STATUS.get(status, "")
        record = records.pop((run_id, channel_id), None)
        anchor_ts = record.message_ts if record is not None else ""
        if not card_status:
            logger.info(
                "[SlackChannel] cron run %s reported status %r; delivering the result without a status card",
                run_id, status,
            )
            return CronPush(run_id, channel_id, False, None, anchor_ts, True)

    card = cron_task_card(cron, card_status=card_status, body=content)
    body_below_card = card_status == _CARD_STATUS_COMPLETE or (
        card_status == _CARD_STATUS_ERROR and len((content or "").strip().splitlines()) > 1
    )
    return CronPush(run_id, channel_id, is_placeholder, card, anchor_ts, body_below_card)
