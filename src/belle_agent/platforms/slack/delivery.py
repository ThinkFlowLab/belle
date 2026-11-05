"""Slack block refusal handling and delivery errors."""

import asyncio
import json
import logging
import re
from collections.abc import Callable, Mapping
from typing import Any

from belle_agent.platforms.slack import blocks as slack_blocks
from belle_agent.platforms.slack.retry import TRANSIENT_ERRORS, retry_after_seconds
from belle_agent.platforms.slack.text import clamp as _clamp

try:
    from aiohttp import ClientError as _ClientError
except ImportError:
    _ClientError = OSError

_BLOCK_REJECTION_ERRORS = frozenset({"invalid_blocks", "invalid_blocks_format", "msg_blocks_too_long"})
_RISKY_BLOCK_KINDS = frozenset({
    slack_blocks.BLOCK_KIND_FENCE,
    slack_blocks.BLOCK_KIND_INTERACTIVE,
    slack_blocks.BLOCK_KIND_UNKNOWN,
})
_RISKY_BLOCK_COUNT = 12
_RISKY_BLOCK_CHARACTERS = 8000
_MAX_REFUSED_PAYLOAD_LENGTH = 2500
_MAX_VALIDATION_ERRORS_SHOWN = 5
_MAX_LOGGED_VALIDATION_MESSAGE = 200
_MAX_LOGGED_REFUSED_BLOCKS = 5
_SEND_REFUSAL_POINTER_RE = re.compile(r"json-pointer:\s*([^\]\s]+)")
_BLOCK_FAILURE_DATA_NOTICE = (
    "_Slack refused the rich rendering of this message, so the data above is"
    " shown as text._"
)
_BLOCK_FAILURE_FENCE_NOTICE = (
    "_Slack refused this Block Kit payload, so it is left here as source."
    " Slack's validator reported:_"
)
_BLOCK_FAILURE_INTERACTIVE_NOTICE = (
    "_Slack refused this message's controls, so there is nothing here to click._"
)


#: How the JiuwenSwarm channel tags its log lines. A caller that is not that channel passes its own tag.
CHANNEL_LOG_TAG = "[SlackChannel]"


def response_ts(response: Any) -> str:
    """Read the timestamp from a Slack response, if present."""
    try:
        return str(response.get("ts") or "").strip()
    except (AttributeError, TypeError):
        return ""


def validation_errors(response: Any) -> list[Mapping[str, Any]]:
    """Read Block Kit schema complaints. Ignore unrelated API failures.

    A recognized refusal without details becomes one error entry so callers
    do not mistake it for approval.
    """
    data = response if isinstance(response, Mapping) else getattr(response, "data", None)
    if not isinstance(data, Mapping):
        return []
    if data.get("ok"):
        return []
    entries = [
        entry
        for entry in (data.get("errors") or [])
        if isinstance(entry, Mapping)
    ]
    code = str(data.get("error") or "").strip()
    pointered = any(entry.get("pointer") for entry in entries)
    if code not in _BLOCK_REJECTION_ERRORS and not pointered:
        return []
    if entries:
        return entries
    return [{"code": code}] if code else []


def validation_refusal_code(response: Any) -> str:
    """Return the validator's top-level error code for logging."""
    data = response if isinstance(response, Mapping) else getattr(response, "data", None)
    if not isinstance(data, Mapping) or data.get("ok"):
        return ""
    return str(data.get("error") or "").strip()


async def validate_blocks(
    client: Any, blocks: list[dict[str, Any]] | None,
    kind: str, logger: logging.Logger, *, tag: str = CHANNEL_LOG_TAG,
) -> tuple[str, list[Mapping[str, Any]]]:
    """Ask Slack's validator without letting transport failures block delivery."""
    if client is None or not should_validate_blocks(blocks, kind):
        return "", []
    try:
        response = await client.api_call(
            "blocks.validate", data={"blocks": json.dumps(blocks, ensure_ascii=False)}
        )
    except Exception as exc:  # noqa: BLE001 - Slack raises on schema refusals
        response = getattr(exc, "response", None)
        errors = validation_errors(response)
        if errors:
            return validation_refusal_code(response), errors
        logger.info(
            "%s blocks.validate did not answer for a %s payload "
            "(%s); sending it unvalidated", tag, kind, exc,
        )
        return "", []
    return validation_refusal_code(response), validation_errors(response)


def _slack_response_data(exc: Exception) -> Mapping[str, Any]:
    """Return the body of a Slack API error, if available."""
    response = getattr(exc, "response", None)
    data = response if isinstance(response, Mapping) else getattr(response, "data", None)
    return data if isinstance(data, Mapping) else {}


def send_refusal_errors(exc: Exception) -> list[Mapping[str, Any]]:
    """Extract validator details from a refused ``chat.postMessage`` call.

    Slack puts these in ``response_metadata.messages`` rather than ``errors``.
    Keep messages even when they contain no JSON pointer.
    """
    data = _slack_response_data(exc)
    metadata = data.get("response_metadata")
    raw = metadata.get("messages") if isinstance(metadata, Mapping) else None
    if not isinstance(raw, (list, tuple)):
        return []
    errors: list[Mapping[str, Any]] = []
    for line in raw:
        message = str(line or "").strip()
        if not message:
            continue
        found = _SEND_REFUSAL_POINTER_RE.search(message)
        errors.append(
            {"pointer": found.group(1) if found else "", "message": message}
        )
    return errors


def _block_identity(block: Any) -> str:
    """Describe a block's structure without logging message text or values."""
    if not isinstance(block, Mapping):
        return "?"
    parts = [str(block.get("type") or "?")]
    block_id = str(block.get("block_id") or "").strip()
    if block_id:
        parts.append(f"block_id={block_id}")
    element = block.get("element")
    if isinstance(element, Mapping):
        parts.append(f"element={element.get('type') or '?'}")
        action_id = str(element.get("action_id") or "").strip()
        if action_id:
            parts.append(f"action_id={action_id}")
    elements = block.get("elements")
    if isinstance(elements, list):
        types = [
            str(item.get("type") or "?")
            for item in elements
            if isinstance(item, Mapping)
        ]
        if types:
            parts.append("elements=" + ",".join(types))
    return " ".join(parts)


def describe_refused_blocks(
    blocks: list[dict[str, Any]], errors: list[Mapping[str, Any]]
) -> str:
    """Name the block types at Slack's error pointers for operator logs."""
    indices = _pointed_indices(blocks, errors)
    if indices:
        listed = indices[:_MAX_LOGGED_REFUSED_BLOCKS]
        described = "; ".join(f"/{index} {_block_identity(blocks[index])}" for index in listed)
        if len(indices) > len(listed):
            described += f"; (+{len(indices) - len(listed)} more)"
        return described
    types = [
        str(block.get("type") or "?")
        for block in blocks
        if isinstance(block, Mapping)
    ]
    return f"no pointer; {len(blocks)} blocks: " + ",".join(types) if types else "no payload"


def validation_error_lines(errors: list[Mapping[str, Any]]) -> list[str]:
    """Format Slack schema complaints as pointer-first bullet lines."""
    lines: list[str] = []
    for entry in errors[:_MAX_VALIDATION_ERRORS_SHOWN]:
        pointer = str(entry.get("pointer") or "").strip()
        message = str(entry.get("message") or "").strip()
        code = str(entry.get("code") or "").strip()
        constraint = str(entry.get("constraint") or "").strip()
        detail = message or constraint or code or "refused"
        if code and detail != code:
            detail = f"{detail} ({code})"
        lines.append(f"• `{pointer}` — {detail}" if pointer else f"• {detail}")
    remaining = len(errors) - len(lines)
    if remaining > 0:
        lines.append(f"• …and {remaining} more")
    return lines


def describe_validation_errors(errors: list[Mapping[str, Any]]) -> str:
    """Format bounded pointer, code, and message details for operator logs."""
    parts: list[str] = []
    for entry in errors[:_MAX_VALIDATION_ERRORS_SHOWN]:
        pointer = str(entry.get("pointer") or "").strip() or "?"
        code = str(entry.get("code") or "").strip()
        message = str(entry.get("message") or "").strip()
        constraint = str(entry.get("constraint") or "").strip()
        detail = code or "refused"
        said = message or constraint
        if said:
            detail = f"{detail} ({_clamp(said, _MAX_LOGGED_VALIDATION_MESSAGE)})"
        parts.append(f"{pointer}={detail}")
    if len(errors) > len(parts):
        parts.append(f"(+{len(errors) - len(parts)} more)")
    return " ".join(parts)


def should_validate_blocks(blocks: list[dict[str, Any]] | None, kind: str) -> bool:
    """Preflight risky Block Kit payloads. Sends still handle API refusals."""
    if not blocks:
        return False
    if kind in _RISKY_BLOCK_KINDS:
        return True
    if len(blocks) >= _RISKY_BLOCK_COUNT:
        return True
    return len(json.dumps(blocks, ensure_ascii=False)) >= _RISKY_BLOCK_CHARACTERS


def pointed_blocks(
    blocks: list[dict[str, Any]], errors: list[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Return blocks named by error pointers, or all blocks without pointers."""
    wanted = _pointed_indices(blocks, errors)
    return [blocks[index] for index in wanted] if wanted else blocks


def _pointed_indices(
    blocks: list[dict[str, Any]], errors: list[Mapping[str, Any]]
) -> list[int]:
    """Read both ``/4/element`` and ``/blocks/4/element`` pointers."""
    wanted: list[int] = []
    for entry in errors:
        parts = str(entry.get("pointer") or "").strip("/").split("/")
        if parts and parts[0] == "blocks":
            parts = parts[1:]
        if parts and parts[0].isdigit():
            index = int(parts[0])
            if 0 <= index < len(blocks) and index not in wanted:
                wanted.append(index)
    return wanted


def block_failure_text(
    *,
    text: str,
    blocks: list[dict[str, Any]],
    kind: str,
    errors: list[Mapping[str, Any]],
    text_route: str = "",
) -> str:
    """What the reader is shown instead of a rendering Slack will not accept.

    Kind-aware, and the kind is the argument rather than something inferred from
    *blocks*, because the payload cannot tell the three apart: a ``section``
    holding an approval prompt and a ``section`` holding a paragraph of an answer
    are the same object.

    The message's own ``text`` always leads. It is a complete rendering of the
    same content on every path that has one, so the notice is an addition to a
    message that still says what it said, never a replacement for it.
    """
    parts = [text.strip()] if text.strip() else []
    if kind == slack_blocks.BLOCK_KIND_INTERACTIVE:
        # No payload, and no pointer either. Both are this connector's
        # internals -- a button's value encodes a request id -- and neither
        # helps a reader whose problem is that the thing they were asked to
        # click is not there. The pointer is in the log, where it is the
        # operator's to act on.
        notice = _BLOCK_FAILURE_INTERACTIVE_NOTICE
        if text_route.strip():
            notice = f"{notice.rstrip('_')} {text_route.strip()}_"
        parts.append(notice)
        return "\n\n".join(parts)

    if kind == slack_blocks.BLOCK_KIND_FENCE:
        parts.append(_BLOCK_FAILURE_FENCE_NOTICE)
        parts.extend(validation_error_lines(errors))
        payload = json.dumps(
            pointed_blocks(blocks, errors), ensure_ascii=False, indent=2
        )
        parts.append(f"```\n{_clamp(payload, _MAX_REFUSED_PAYLOAD_LENGTH)}\n```")
        return "\n\n".join(parts)

    # Data, chrome, and anything unclassified. The reader asked for numbers, so
    # the numbers are rebuilt from the blocks themselves and the notice comes
    # last, under them: it explains what they are looking at rather than
    # standing between them and it.
    rebuilt = slack_blocks.data_fallback_text(blocks, already_in=text)
    if rebuilt:
        parts.append(rebuilt)
    parts.append(_BLOCK_FAILURE_DATA_NOTICE)
    return "\n\n".join(parts)


def rejected_blocks_error(exc: Exception) -> str | None:
    """Return the error code if *exc* is Slack refusing the blocks, else ``None``.

    Narrow on purpose. These three say the structured rendering was unacceptable
    and say nothing about the message being undeliverable, so the same payload's
    ``text`` is still worth sending. Everything else -- ``channel_not_found``,
    ``invalid_auth``, ``ratelimited`` -- is about the message or the caller, and
    retrying it without blocks would only delay the error the caller needs.
    """
    data = _slack_response_data(exc)
    error = data.get("error")
    return error if error in _BLOCK_REJECTION_ERRORS else None


def slack_error_code(exc: Exception) -> str:
    """Return Slack's response error code, or empty for transport failures."""
    data = _slack_response_data(exc)
    return str(data.get("error") or "").strip()


def slack_transient(exc: Exception) -> bool:
    """Whether the failed call can succeed when made again: Slack did not answer, or said it cannot serve now."""
    if code := slack_error_code(exc):
        return code in TRANSIENT_ERRORS
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status >= 500 or status == 429
    # A connection that fails or times out is an OSError, and one that closes early an aiohttp ClientError.
    return isinstance(exc, (OSError, _ClientError))


def slack_failure_reason(exc: Exception) -> str:
    """Put Slack's error code first and fold exception text onto one log line."""
    code = slack_error_code(exc)
    detail = " ".join(str(exc).split()) or exc.__class__.__name__
    return f"{code}: {detail}" if code else detail


def short_echo_tail(
    *,
    response: Any,
    channel_id: str,
    response_ts: str,
    sent_text: str,
    update_ts: str,
    had_blocks: bool = False,
    logger: logging.Logger,
    tag: str = CHANNEL_LOG_TAG,
) -> str:
    """Return text Slack truncated, unless the full Block Kit rendering arrived."""
    if response is None:
        return ""
    try:
        echoed = response.get("message") or {}
        echoed_text = str(echoed.get("text") or "")
    except (AttributeError, TypeError):
        return ""
    if not echoed_text or len(echoed_text) >= len(sent_text):
        return ""

    stored = echoed_text.rstrip()
    sent = sent_text.rstrip()
    where = (
        tag,
        channel_id,
        response_ts or "-",
        "update" if update_ts else "post",
        len(sent_text),
        len(echoed_text),
    )
    if sent.endswith(stored):
        logger.info(
            "%s Slack split what was sent into messages of its "
            "own: channel=%s ts=%s mode=%s sent_chars=%d last_chars=%d",
            *where,
        )
        return ""
    if not sent.startswith(stored):
        logger.warning(
            "%s Slack echoed text that is neither the start nor "
            "the end of what was sent, so what reached the reader cannot be "
            "placed: channel=%s ts=%s mode=%s sent_chars=%d stored_chars=%d",
            *where,
        )
        return ""

    logger.warning(
        "%s Slack stored less text than was sent: channel=%s ts=%s mode=%s sent_chars=%d stored_chars=%d",
        *where,
    )
    if had_blocks:
        logger.info(
            "%s the text Slack cut is the fallback beside a "
            "Block Kit rendering that arrived whole, so nothing is posted "
            "back: channel=%s ts=%s",
            tag,
            channel_id,
            response_ts or "-",
        )
        return ""
    return sent_text[len(stored) :]


def drop_refused_blocks(kwargs: dict[str, Any], *, update_ts: str) -> None:
    """Clear blocks on edits; omit them on new posts.

    Omitting blocks from ``chat.update`` preserves the previous rendering.
    """
    if update_ts:
        kwargs["blocks"] = []
    else:
        kwargs.pop("blocks", None)


async def post_payload(
    client: Any, kwargs: dict[str, Any], *, update_ts: str,
    blocks: list[dict[str, Any]] | None,
    fallback_text: Callable[[list[dict[str, Any]], list[Mapping[str, Any]]], str],
    logger: logging.Logger, retry_limit: int = 3, tag: str = CHANNEL_LOG_TAG,
) -> tuple[Any, list[dict[str, Any]] | None, str]:
    """Retry Slack posts and edits; retry refused Block Kit as plain text."""
    retries = 0
    refused_blocks = list(blocks) if blocks else []
    while True:
        try:
            method = client.chat_update if update_ts else client.chat_postMessage
            response = await method(**kwargs)
        except Exception as exc:  # noqa: BLE001 - Slack SDK errors vary
            delay = retry_after_seconds(exc)
            if delay is not None and retries < retry_limit:
                retries += 1
                logger.warning(
                    "Slack rate limited; retrying in %.1fs (%d/%d)",
                    delay, retries, retry_limit,
                )
                await asyncio.sleep(delay)
                continue
            rejected = rejected_blocks_error(exc)
            if rejected is not None and kwargs.get("blocks"):
                details = send_refusal_errors(exc)
                logger.warning(
                    "Slack rejected the Block Kit rendering (%s); Slack "
                    "reported: %s; the payload it named: %s; delivering "
                    "this chunk as plain text",
                    rejected,
                    describe_validation_errors(details) if details else "no detail",
                    describe_refused_blocks(
                        list(refused_blocks or kwargs.get("blocks") or []), details,
                    ),
                )
                refused = list(refused_blocks or kwargs["blocks"] or [])
                drop_refused_blocks(kwargs, update_ts=update_ts)
                blocks = None
                if refused:
                    kwargs["text"] = fallback_text(refused, [{"code": rejected}])
                continue
            reason = slack_failure_reason(exc)
            logger.warning("%s send failed: %s", tag.strip("[]"), reason)
            return None, blocks, reason
        return response, blocks, ""


class SlackDeliveryError(RuntimeError):
    """Report failed delivery, including how many parts were already posted."""

    def __init__(
        self,
        reason: str,
        *,
        channel_id: str = "",
        chunks_sent: int = 0,
        chunks_total: int = 0,
        unit: str = "chunks",
    ) -> None:
        self.reason = reason
        self.channel_id = channel_id
        self.chunks_sent = chunks_sent
        self.chunks_total = chunks_total
        target = f" to {channel_id}" if channel_id else ""
        progress = (
            f" after {chunks_sent}/{chunks_total} {unit}"
            if chunks_sent and chunks_total > 1
            else ""
        )
        super().__init__(f"Slack delivery{target} failed{progress}: {reason}")
