"""Ingest Slack links and attachments through the connector ingestion hook."""

import asyncio
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from belle_agent.platforms.base import MARK_INGESTING
from belle_agent.platforms.slack.file_transfer import event_entries
from belle_agent.platforms.slack.identity import event_chat_type
from belle_agent.platforms.slack.scoped_config import CHAT_TYPE_PUBLIC_CHANNEL
from belle_agent.second_brain.hosted import STATUS_FAILED, ingest_mark
from belle_agent.second_brain.ingest import STATUS_DUPLICATE, STATUS_FILTERED, STATUS_INGESTED
from belle_agent.second_brain.links import extract_urls

logger = logging.getLogger(__name__)


def _slack_ts_to_iso(message_ts: str) -> str:
    try:
        seconds = float(str(message_ts).strip())
        return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def brain_session_id(event: Mapping[str, Any]) -> str:
    team = str(event.get("team") or "default").strip() or "default"
    channel_id = str(event.get("channel") or "unknown").strip() or "unknown"
    return f"slack_brain_{team}_{channel_id}"


def _ingests_from_channel(channel: Any, channel_id: str, event: Mapping[str, Any]) -> bool:
    if channel.config.brain_ingest_all_channels:
        return True
    if not channel_id:
        return False
    if channel_id in channel.config.brain_ingest_channels:
        return True
    return (
        channel.config.brain_ingest_public_channels
        and event_chat_type(event) == CHAT_TYPE_PUBLIC_CHANNEL
    )


def maybe_dispatch_brain_ingest(channel: Any, event: dict[str, Any]) -> bool:
    """Start detached ingestion when this conversation is configured."""
    if channel._brain_ingest_hook is None:
        return False
    if not _ingests_from_channel(channel, str(event.get("channel") or "").strip(), event):
        return False

    attachments = event_entries(event, "files")
    urls = extract_urls(str(event.get("text") or ""))
    if not attachments and not urls:
        return False

    task = asyncio.create_task(run_brain_ingest(channel, event, attachments, urls))
    channel._brain_tasks.add(task)
    task.add_done_callback(channel._brain_tasks.discard)
    return True


async def run_brain_ingest(
    channel: Any,
    event: dict[str, Any],
    attachments: list[dict[str, Any]],
    urls: list[str],
) -> None:
    """Ingest message files and links, then react with the outcome."""
    channel_id = str(event.get("channel") or "").strip()
    message_ts = str(event.get("ts") or "").strip()
    event_id = str(event.get("event_id") or event.get("client_msg_id") or "")
    dedupe_key = ":".join(
        part for part in ("brain", channel_id, event_id or message_ts) if part
    )
    if dedupe_key and not await channel._remember_event(dedupe_key):
        logger.debug("[SlackChannel] brain ingest already handled: %s", dedupe_key)
        return

    await channel._react_for_state(channel_id, message_ts, MARK_INGESTING)
    statuses: list[str] = []
    try:
        meta = await _brain_meta(channel, event)
        if attachments:
            downloaded, failures = await channel._download_attachments(
                attachments, brain_session_id(event)
            )
            statuses += [STATUS_FAILED] * len(failures)
            for item in downloaded:
                path = str(item.get("path") or "")
                statuses.append(await _offer_to_brain(
                    channel,
                    kind="file",
                    meta=meta,
                    path=path,
                    filename=str(item.get("original_filename") or item.get("filename") or ""),
                    mime_type=str(item.get("mime_type") or ""),
                ))
                await _discard_staged(path)

        for url in urls:
            statuses.append(await _offer_to_brain(channel, kind="link", meta=meta, url=url))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("[SlackChannel] second-brain ingest raised: %s", exc)  # noqa: TRY401
        statuses.append(STATUS_FAILED)

    await end_brain_mark(channel, channel_id, message_ts, ingest_mark(statuses))


async def end_brain_mark(channel: Any, channel_id: str, message_ts: str, mark: str) -> None:
    """Replace the pending reaction of an ingest with the one ``mark`` selects, if any."""
    await channel._react_for_state(channel_id, message_ts, mark)


async def _discard_staged(path: str) -> None:
    if not path:
        return
    try:
        await asyncio.to_thread(Path(path).unlink, missing_ok=True)
    except OSError as exc:
        logger.debug("[SlackChannel] could not remove staged %s: %s", path, exc)


async def _offer_to_brain(
    channel: Any,
    *,
    kind: str,
    meta: Mapping[str, Any],
    path: str = "",
    filename: str = "",
    mime_type: str = "",
    url: str = "",
) -> str:
    if channel._brain_ingest_hook is None:
        return STATUS_FAILED

    params: dict[str, Any] = {"kind": kind, "slack": dict(meta)}
    if kind == "link":
        params["url"] = url
    else:
        params.update(path=path, filename=filename, mime_type=mime_type)

    try:
        payload = await channel._brain_ingest_hook(params)
    except Exception as exc:  # noqa: BLE001 - ingestion failures are reported as outcomes
        logger.warning(
            "[SlackChannel] second-brain ingest failed for %s: %s",
            url or filename or path,
            exc,
        )
        return STATUS_FAILED

    status = str((payload or {}).get("status") or "")
    if status == STATUS_FILTERED:
        logger.info(
            "[SlackChannel] second brain declined %s: %s",
            url or filename,
            (payload or {}).get("reason") or "filtered",
        )
    elif status not in (STATUS_INGESTED, STATUS_DUPLICATE):
        logger.warning(
            "[SlackChannel] second brain returned an unknown status %r for %s",
            status,
            url or filename,
        )
    return status


async def _brain_meta(channel: Any, event: Mapping[str, Any]) -> dict[str, Any]:
    channel_id = str(event.get("channel") or "").strip()
    message_ts = str(event.get("ts") or "").strip()
    meta: dict[str, Any] = {
        "slack_channel": channel_id,
        "poster": str(event.get("user") or "").strip(),
        "posted_at": _slack_ts_to_iso(message_ts),
        "comment": str(event.get("text") or "").strip(),
    }
    permalink = await _brain_permalink(channel._client, channel_id, message_ts)
    if permalink:
        meta["slack_permalink"] = permalink
    return meta


async def _brain_permalink(client: Any, channel_id: str, message_ts: str) -> str:
    if client is None or not channel_id or not message_ts:
        return ""
    try:
        response = await client.chat_getPermalink(
            channel=channel_id, message_ts=message_ts
        )
    except Exception as exc:  # noqa: BLE001 - ingestion failures are reported as outcomes
        logger.debug("[SlackChannel] chat.getPermalink failed: %s", exc)
        return ""
    return str((response or {}).get("permalink") or "")
