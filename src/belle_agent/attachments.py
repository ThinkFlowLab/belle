"""Load the files a message carries, within one deadline, and say which ones could not be read.

A platform supplies how one file is fetched and named. The deadline, the
reason given for each file that is left out, and the notice that tells the
sender are the same on every platform.
"""

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from belle_agent.backend import ContentPart, ToolContent

#: The most bytes this bot reads of one file. A deployment's own ceiling, not a platform's limit.
MAX_FILE_BYTES = 30 * 1024 * 1024

FILE_REASON_TOO_LARGE = "the file is over the size limit this bot will download"
FILE_REASON_TIMED_OUT = "the download took too long and was given up on"

_FILE_NOTICE_MAX_NAMES = 5


class AttachmentUnreadable(RuntimeError):
    """A file of a message that could not be read. ``reason`` is safe to tell its sender."""

    def __init__(self, detail: str, reason: str) -> None:
        self.reason = reason
        super().__init__(detail)


class FileTextOverLimit(Exception):
    """A file's text is longer than one tool result may hold."""

    def __init__(self, chars: int) -> None:
        super().__init__(f"{chars} characters")
        self.chars = chars


class FileSourceRefused(Exception):
    """A file to send that a call does not give in exactly one usable form. ``code`` and ``detail`` say why."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def outgoing_file(
    content: str | None, content_base64: str | None, file_id: str | None, filename: str, *,
    allow_empty: bool = False,
) -> bytes | None:
    """The bytes of a file a call gives inline, or ``None`` where it names a file by ``file_id`` instead.

    Raise ``FileSourceRefused`` where the call gives none or several of the
    three, gives content with no ``filename``, or gives base64 that does not decode.
    """
    given = [name for name, value in (("content", content), ("content_base64", content_base64))
             if value is not None and (allow_empty or bool(value))]
    if file_id:
        given.append("file_id")
    if len(given) != 1:
        raise FileSourceRefused(
            "file_source_required",
            "give the file in exactly one of content, content_base64 and file_id;"
            f" this call gives {', '.join(given) or 'none of them'}",
        )
    if file_id:
        return None
    if not filename:
        raise FileSourceRefused("filename_required", "a file given as content needs a filename, with its extension")
    if content is not None:
        return content.encode("utf-8")
    try:
        return base64.b64decode(str(content_base64), validate=True)
    except (binascii.Error, ValueError):
        raise FileSourceRefused("content_base64_malformed", "content_base64 is not valid base64") from None


def file_content(record: Mapping[str, Any], data: bytes, *, name: str, max_chars: int) -> str | ToolContent:
    """A file read by a tool, as its result: a text file's text in ``content``, any other file beside the record.

    ``record`` describes the file, with its media type in ``mimetype``. Raise
    ``FileTextOverLimit`` where the text is longer than ``max_chars``: no part
    of a text is returned as if it were the whole.
    """
    text = None
    if b"\x00" not in data:
        with contextlib.suppress(UnicodeDecodeError):
            text = data.decode("utf-8")
    if text is None:
        # Not text, so not held to the text budget. The tool host returns it where the backend hands
        # that type to the model, and refuses otherwise.
        described = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        mime = str(record.get("mimetype") or "application/octet-stream")
        return ToolContent(text=described, parts=(ContentPart(mime=mime, data=data, name=name),))
    if len(text) > max_chars:
        raise FileTextOverLimit(len(text))
    return json.dumps({**record, "content": text}, ensure_ascii=False, separators=(",", ":"))


async def load_attachments(
    files: list[dict[str, Any]],
    download: Callable[[Mapping[str, Any]], Awaitable[dict[str, Any]]],
    *,
    name: Callable[[Mapping[str, Any]], str],
    budget_seconds: float,
    logger: logging.Logger,
    label: str,
    failed_reason: str,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Fetch each file within one deadline. Return the records, and each file left out with its reason.

    ``download`` fetches one file and returns its record. It raises
    ``AttachmentUnreadable`` with the reason to give. Any other error gives
    ``failed_reason``. ``label`` names a file in the log lines.
    """
    downloaded: list[dict[str, Any]] = []
    failures: list[tuple[str, str]] = []
    deadline = time.monotonic() + budget_seconds
    for file_info in files:
        shown = name(file_info)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning("%s %r not read: no time left in the download budget", label, shown)
            failures.append((shown, FILE_REASON_TIMED_OUT))
            continue
        try:
            record = await asyncio.wait_for(download(file_info), remaining)
        except TimeoutError:
            logger.warning("%s %r not read: download exceeded the %.0fs budget", label, shown, budget_seconds)
            failures.append((shown, FILE_REASON_TIMED_OUT))
        except AttachmentUnreadable as exc:
            logger.warning("%s %r not read: %s", label, shown, exc)
            failures.append((shown, exc.reason))
        except Exception as exc:
            logger.warning("%s %r failed to download: %s", label, shown, exc, exc_info=True)
            failures.append((shown, failed_reason))
        else:
            downloaded.append(record)
    return downloaded, failures


async def fetch_message_files(
    files: list[dict[str, Any]],
    download: Callable[[Mapping[str, Any]], Awaitable[dict[str, Any]]],
    *,
    name: Callable[[Mapping[str, Any]], str],
    budget_seconds: float,
    logger: logging.Logger,
    label: str,
    failed_reason: str,
    conversation: str,
    notify: Callable[[str], Awaitable[None]],
    log_prefix: str = "",
) -> tuple[Mapping[str, Any], ...]:
    """Load files and notify the sender once per failure reason."""
    loaded, failures = await load_attachments(
        files, download, name=name, budget_seconds=budget_seconds, logger=logger, label=label, failed_reason=failed_reason
    )
    reasons: dict[str, list[str]] = {}
    for shown, reason in failures:
        logger.warning("%s: not loaded %s from %s reason=%s", log_prefix or label.lower(), shown, conversation, reason)
        reasons.setdefault(reason, []).append(shown)
    for reason, names in reasons.items():
        await notify(unreadable_file_notice(names, reason))
    return tuple(loaded)


def unreadable_file_notice(names: list[str], reason: str) -> str:
    """Tell the reader why one or more attachment downloads failed."""
    shown = names[:_FILE_NOTICE_MAX_NAMES]
    listed = ", ".join(shown)
    hidden = len(names) - len(shown)
    if hidden > 0:
        listed = f"{listed} (+{hidden} more)"
    singular = len(names) == 1
    return (
        f"Received {len(names)} "
        f"{'attachment' if singular else 'attachments'} ({listed}) but could"
        f" not read {'it' if singular else 'them'}: {reason}."
    )
