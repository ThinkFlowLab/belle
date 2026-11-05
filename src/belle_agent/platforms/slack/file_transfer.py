"""Validate hosts and pin addresses for Slack file downloads."""

import asyncio
import ipaddress
import logging
import re
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from belle_agent.attachments import (
    FILE_REASON_TIMED_OUT,
    FILE_REASON_TOO_LARGE,
    MAX_FILE_BYTES,
    AttachmentUnreadable,
    load_attachments,
    unreadable_file_notice,
)
from belle_agent.platforms.slack.delivery import slack_error_code, slack_failure_reason
from belle_agent.platforms.slack.retry import retry_after_seconds
from belle_agent.platforms.slack.rich_text_walk import RICH_TEXT_KEYS, walk_strings
from belle_agent.platforms.slack.text import clamp

# Callers may also allow the workspace host verified by Slack auth.test.
SLACK_FILE_HOST = "files.slack.com"

FILE_TRANSFER_TIMEOUT_SECONDS = 60.0

FILE_REASON_DOWNLOAD_FAILED = "the download from Slack failed"
FILE_REASON_UNAUTHORIZED = "Slack refused the download; the bot token is missing the files:read scope"
FILE_REASON_OFF_SLACK = (
    "the file is stored outside Slack, so the bot did not fetch it; open it from its permalink in Slack instead"
)
FILE_REASON_UNSAFE_ROUTE = (
    "the download would not have reached Slack, so the bot did not fetch it;"
    " open the file from its permalink in Slack instead"
)

_ATTACHMENT_TEXT_KEYS = RICH_TEXT_KEYS | {"author_name", "service_name", "title_link", "value"}
_MAX_ATTACHMENT_TEXT_CHARS = 2000

UNSAFE_PATH_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_path_component(value: str, fallback: str) -> str:
    """Reduce a Slack-supplied filename to one safe path component."""
    name = Path(str(value or "")).name.strip()
    if not name or name in {".", ".."}:
        name = fallback
    return UNSAFE_PATH_CHARS_RE.sub("_", name)[:180] or fallback


REFUSAL_HOST_NOT_ALLOWED = "slack_file_host_not_allowed"

REFUSAL_HOST_UNRESOLVED = "slack_file_host_did_not_resolve"

REFUSAL_ADDRESS_REFUSED = "slack_file_address_refused"

REFUSAL_TOO_MANY_REDIRECTS = "slack_file_too_many_redirects"

MAX_FILE_REDIRECTS = 3

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

MAX_DIALLED_ADDRESSES = 4

_ADDRESS_REFUSALS: tuple[tuple[str, str], ...] = (
    ("is_unspecified", "the unspecified address"),
    ("is_loopback", "a loopback address"),
    ("is_link_local", "a link-local address"),
    ("is_multicast", "a multicast address"),
    ("is_reserved", "a reserved address"),
    ("is_private", "a private address"),
)


class SlackFileTransferRefused(RuntimeError):
    """A download rejected by policy, with a stable code and readable detail."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class SlackAttachmentError(AttachmentUnreadable):
    """An inbound file could not be read; reason is safe to tell the user."""


def https_host(url: Any) -> str:
    """Return a lowercase HTTPS host, or ``""``."""
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return ""
    if parsed.scheme != "https":
        return ""
    return (parsed.hostname or "").strip().lower()


def https_file_host(url: Any) -> str:
    """Return a lowercase HTTPS host without its trailing dot, or ``""``."""
    return https_host(url).rstrip(".")


def normalised_hosts(hosts: Iterable[str]) -> frozenset[str]:
    """Normalize allowed HTTPS hostnames."""
    return frozenset(host.strip().lower().rstrip(".") for host in hosts if host and host.strip())


def address_refusal_reason(address: Any) -> str | None:
    """Return why an address is non-public, including IPv4-mapped IPv6."""
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    for attribute, phrase in _ADDRESS_REFUSALS:
        if getattr(address, attribute, False):
            return phrase
    return None


def _getaddrinfo(hostname: str, port: int) -> list[Any]:
    """Resolve a host for validation."""
    return socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)


def resolve_validated_addresses(host: str, port: int) -> tuple[str, ...]:
    """Reject non-public DNS answers before capping the pinned address list."""
    hostname = (host or "").strip().lower().rstrip(".")
    if not hostname:
        raise SlackFileTransferRefused(
            REFUSAL_HOST_UNRESOLVED,
            "download URL has no hostname",
        )
    try:
        answers = _getaddrinfo(hostname, port)
    except OSError as exc:
        raise SlackFileTransferRefused(
            REFUSAL_HOST_UNRESOLVED,
            f"{hostname} did not resolve ({type(exc).__name__})",
        ) from None

    addresses: list[str] = []
    for answer in answers:
        raw = str(answer[4][0]).split("%")[0]
        try:
            parsed = ipaddress.ip_address(raw)
        except ValueError:
            raise SlackFileTransferRefused(
                REFUSAL_ADDRESS_REFUSED,
                f"{hostname} resolved to invalid IP address {raw!r}",
            ) from None
        reason = address_refusal_reason(parsed)
        if reason is not None:
            raise SlackFileTransferRefused(
                REFUSAL_ADDRESS_REFUSED,
                f"{hostname} resolved to {raw}: {reason}",
            )
        if raw not in addresses:
            addresses.append(raw)

    if not addresses:
        raise SlackFileTransferRefused(
            REFUSAL_HOST_UNRESOLVED,
            f"{hostname} resolved to no addresses",
        )
    return tuple(addresses[:MAX_DIALLED_ADDRESSES])


class _PinnedBackend:
    """Dial validated addresses while preserving TLS host verification."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        addresses = await asyncio.to_thread(resolve_validated_addresses, host, port)
        last: BaseException | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:  # noqa: BLE001
                last = exc
        if last is None:
            raise SlackFileTransferRefused(
                REFUSAL_HOST_UNRESOLVED,
                f"{host} produced no address to dial",
            )
        raise last

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> Any:
        raise SlackFileTransferRefused(
            REFUSAL_ADDRESS_REFUSED,
            "Slack file downloads cannot use Unix sockets",
        )


class _SlackFileTransport(httpx.AsyncHTTPTransport):
    """Require HTTPS, an allowed host, and a pinned address per request."""

    def __init__(self, allowed_hosts: Iterable[str]) -> None:
        super().__init__()
        self._allowed_hosts = normalised_hosts(allowed_hosts)
        pool = getattr(self, "_pool", None)
        backend = getattr(pool, "_network_backend", None)
        if pool is None or backend is None:
            raise SlackFileTransferRefused(
                REFUSAL_ADDRESS_REFUSED,
                "httpx network backend is unavailable; cannot pin the download address",
            )
        pool._network_backend = _PinnedBackend(backend)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = (request.url.host or "").strip().lower().rstrip(".")
        if request.url.scheme != "https" or host not in self._allowed_hosts:
            raise SlackFileTransferRefused(
                REFUSAL_HOST_NOT_ALLOWED,
                "Slack file request must use HTTPS and an allowed host",
            )
        return await super().handle_async_request(request)


def slack_file_transport(allowed_hosts: Iterable[str]) -> httpx.AsyncBaseTransport:
    """Create a pinned transport without proxy routing."""
    return _SlackFileTransport(allowed_hosts)


def slack_file_client(allowed_hosts: Iterable[str]) -> httpx.AsyncClient:
    """Use the pinned transport for Slack file downloads."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(FILE_TRANSFER_TIMEOUT_SECONDS),
        follow_redirects=False,
        transport=slack_file_transport(allowed_hosts),
    )


@asynccontextmanager
async def stream_slack_file(
    client: Any,
    url: str,
    *,
    headers: dict[str, str],
    allowed_hosts: Iterable[str],
) -> AsyncIterator[Any]:
    """Validate each hop and yield the first non-redirect response open."""
    hosts = normalised_hosts(allowed_hosts)
    target = str(url)
    for hop in range(MAX_FILE_REDIRECTS + 1):
        if https_file_host(target) not in hosts:
            where = "the download URL" if hop == 0 else f"redirect hop {hop}"
            raise SlackFileTransferRefused(
                REFUSAL_HOST_NOT_ALLOWED,
                f"{where} must use HTTPS and an allowed host",
            )
        async with client.stream("GET", target, headers=headers) as response:
            if response.status_code not in _REDIRECT_STATUSES or not (
                location := str(response.headers.get("location") or "").strip()
            ):
                yield response
                return
        target = urljoin(target, location)

    raise SlackFileTransferRefused(
        REFUSAL_TOO_MANY_REDIRECTS,
        f"download exceeded {MAX_FILE_REDIRECTS} redirects",
    )


def _first_nonempty_field(file_info: Mapping[str, Any], keys: tuple[str, ...], fallback: str = "") -> str:
    for key in keys:
        value = str(file_info.get(key) or "").strip()
        if value:
            return value
    return fallback


def slack_file_name(file_info: Mapping[str, Any]) -> str:
    """Use the uploader's filename, then title or file ID."""
    return _first_nonempty_field(file_info, ("name", "title", "id"), "unnamed file")


def event_entries(event: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    """Read mapping entries from a Slack event list."""
    raw = event.get(key)
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def outgoing_files(payload: Any) -> list[tuple[str, str]]:
    """Read paths and filenames from a chat.file payload."""
    raw = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    files = []
    for entry in raw:
        if isinstance(entry, Mapping):
            path = str(entry.get("path") or "").strip()
            name = str(entry.get("name") or "").strip()
        else:
            path, name = str(entry or "").strip(), ""
        if path:
            files.append((path, name or Path(path).name))
    return files


def attachment_params(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Group uploads by media type for the agent request."""
    images = [record for record in records if record["type"] == "image"]
    documents = [record for record in records if record["type"] != "image"]
    return {
        **({"uploaded_images": images} if images else {}),
        **({"uploaded_documents": documents} if documents else {}),
    }


def describe_downloaded_attachments(text: str, records: list[dict[str, Any]]) -> str:
    """Include local file paths in the agent's message text."""
    lines = [
        f"- {record['filename']}"
        + (f" ({record['mime_type']})" if record["mime_type"] else "")
        + f": {record['path']}"
        for record in records
    ]
    header = "Attached file, saved locally:" if len(records) == 1 else f"{len(records)} attached files, saved locally:"
    block = "\n".join([header, *lines])
    return f"{text}\n\n{block}" if text else block


def describe_event_attachments(text: str, attachments: list[dict[str, Any]]) -> str:
    """Summarize visible attachment text separately from the user's text."""
    lifted = walk_strings(attachments, _ATTACHMENT_TEXT_KEYS)
    count = len(attachments)
    noun = "Attachment" if count == 1 else f"{count} attachments"
    header = f"{noun} shown on this message:" if lifted else f"{noun} shown on this message, with no readable text."
    block = clamp("\n".join([header, *lifted]), _MAX_ATTACHMENT_TEXT_CHARS)
    return f"{text}\n\n{block}" if text else block



def private_url(file_info: Mapping[str, Any]) -> str:
    """Prefer the authenticated download URL."""
    return _first_nonempty_field(file_info, ("url_private_download", "url_private"))


def unique_path(path: Path) -> Path:
    """Avoid overwriting a same-named file in the supplied directory."""
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem}-overflow{path.suffix}")


async def resolve_attachment(client: Any, file_info: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    """Find a private URL, using files.info if the event omitted it."""
    url = private_url(file_info)
    if url:
        return url, file_info

    file_id = str(file_info.get("id") or "").strip()
    if not file_id or client is None:
        raise SlackAttachmentError("no private URL and no file id to look one up with", FILE_REASON_DOWNLOAD_FAILED)
    try:
        response = await client.files_info(file=file_id)
    except Exception as exc:
        raise SlackAttachmentError(
            f"files.info for {file_id} failed: {slack_failure_reason(exc)}",
            FILE_REASON_UNAUTHORIZED if slack_error_code(exc) == "missing_scope" else FILE_REASON_DOWNLOAD_FAILED,
        ) from exc
    refreshed = response.get("file") if response is not None else None
    if not isinstance(refreshed, Mapping):
        raise SlackAttachmentError(f"files.info returned no file object for {file_id}", FILE_REASON_DOWNLOAD_FAILED)
    url = private_url(refreshed)
    if not url:
        raise SlackAttachmentError(f"files.info returned no private URL for {file_id}", FILE_REASON_DOWNLOAD_FAILED)
    return url, refreshed


def assert_slack_file_url(url: str, allowed_hosts: Iterable[str]) -> None:
    """Reject off-Slack URLs before attaching the bot token."""
    if https_host(url) in allowed_hosts:
        return
    # Do not include the attacker-supplied URL in the error.
    raise SlackAttachmentError("the file's download URL is not on a Slack file host", FILE_REASON_OFF_SLACK)


async def fetch_attachment_bytes(
    url: str, mimetype: str, *, bot_token: str, allowed_hosts: Iterable[str], max_bytes: int | None = None,
) -> bytearray:
    """Download within a size cap through the pinned Slack transport."""
    hosts = frozenset(allowed_hosts)
    assert_slack_file_url(url, hosts)
    if not bot_token:
        raise SlackAttachmentError("no bot token configured", FILE_REASON_UNAUTHORIZED)
    limit = MAX_FILE_BYTES if max_bytes is None else max_bytes

    headers = {"Authorization": f"Bearer {bot_token}"}
    async with (
        slack_file_client(hosts) as client,
        stream_slack_file(client, url, headers=headers, allowed_hosts=hosts) as response,
    ):
        if response.status_code in (401, 403):
            raise SlackAttachmentError(f"HTTP {response.status_code}", FILE_REASON_UNAUTHORIZED)
        if response.status_code != 200:
            raise SlackAttachmentError(f"HTTP {response.status_code}", FILE_REASON_DOWNLOAD_FAILED)
        content_type = str(response.headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type == "text/html" and mimetype != "text/html":
            raise SlackAttachmentError("Slack served its sign-in page instead of the file", FILE_REASON_UNAUTHORIZED)

        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > limit:
                raise SlackAttachmentError(f"over platforms.slack.file_download_max_bytes ({limit} bytes)", FILE_REASON_TOO_LARGE)
            body.extend(chunk)
    return body


async def download_attachment(
    client: Any,
    file_info: Mapping[str, Any],
    upload_dir: Path,
    *,
    bot_token: str,
    allowed_hosts: Iterable[str],
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """Save one Slack file in a directory selected by the connector."""
    url, resolved = await resolve_attachment(client, file_info)
    mimetype = str(resolved.get("mimetype") or "").strip().lower()
    name = slack_file_name(resolved)
    data = await fetch_attachment_bytes(
        url, mimetype, bot_token=bot_token, allowed_hosts=allowed_hosts, max_bytes=max_bytes,
    )

    upload_dir.mkdir(parents=True, exist_ok=True)
    file_id = str(resolved.get("id") or "").strip()
    path = unique_path(upload_dir / safe_path_component(name, file_id or "slack-attachment"))
    path.write_bytes(data)
    return {
        "type": "image" if mimetype.startswith("image/") else "document",
        "filename": path.name,
        "original_filename": name,
        "path": str(path),
        "mime_type": mimetype,
        "size_bytes": len(data),
        "slack_file_id": file_id,
    }


ATTACHMENT_LABEL = "Slack attachment"


def route_guarded(
    download: Callable[[Mapping[str, Any]], Awaitable[dict[str, Any]]],
) -> Callable[[Mapping[str, Any]], Awaitable[dict[str, Any]]]:
    """Convert unsafe-route refusals into unreadable attachments."""

    async def guarded(file_info: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return await download(file_info)
        except SlackFileTransferRefused as refusal:
            raise SlackAttachmentError(f"{refusal.code} -- {refusal.detail}", FILE_REASON_UNSAFE_ROUTE) from None

    return guarded


async def download_attachments(
    files: list[dict[str, Any]],
    download: Callable[[Mapping[str, Any]], Awaitable[dict[str, Any]]],
    *,
    budget_seconds: float,
    logger: logging.Logger,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Fetch files within one deadline. Return records and per-file failures.

    ``download`` fetches one file to wherever its caller keeps files, and returns its record.
    """
    return await load_attachments(
        files,
        route_guarded(download),
        name=slack_file_name,
        budget_seconds=budget_seconds,
        logger=logger,
        label=ATTACHMENT_LABEL,
        failed_reason=FILE_REASON_DOWNLOAD_FAILED,
    )


def uploaded_file_id(response: Any) -> str:
    """Read the completed upload's file ID, if Slack included it."""
    if response is None:
        return ""
    try:
        entries = response.get("files")
        entry = entries[0] if isinstance(entries, list) and entries else None
        if entry is None:
            entry = response.get("file")
        if not isinstance(entry, Mapping):
            return ""
        return str(entry.get("id") or "").strip()
    except (AttributeError, TypeError, IndexError, KeyError):
        return ""


async def upload_file(
    client: Any,
    *,
    channel_id: str,
    thread_ts: str,
    path: str,
    filename: str,
    file_index: int = 0,
    file_total: int = 0,
    retry_limit: int = 3,
    logger: logging.Logger,
) -> tuple[bool, str]:
    """Upload one file, returning a delivery result for the connector's batch."""
    kwargs: dict[str, Any] = {"channel": channel_id, "file": path, "filename": filename}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    try:
        size_bytes = Path(path).stat().st_size
    except OSError:
        size_bytes = -1

    for attempt in range(retry_limit + 1):
        try:
            response = await client.files_upload_v2(**kwargs)
        except Exception as exc:  # noqa: BLE001
            delay = retry_after_seconds(exc)
            if delay is not None and attempt < retry_limit:
                logger.warning(
                    "Slack rate limited on upload; retrying in %.1fs (%d/%d)", delay, attempt + 1, retry_limit
                )
                await asyncio.sleep(delay)
                continue
            reason = slack_failure_reason(exc)
            logger.warning("SlackChannel file upload failed: %s", reason)
            return False, reason
        logger.info(
            "[SlackChannel] delivered file: channel=%s filename=%s bytes=%d file_id=%s file=%d/%d",
            channel_id,
            filename,
            size_bytes,
            uploaded_file_id(response) or "-",
            file_index,
            file_total,
        )
        return True, ""
    return False, "rate limited"


__all__ = [
    "FILE_REASON_DOWNLOAD_FAILED",
    "FILE_REASON_OFF_SLACK",
    "FILE_REASON_TIMED_OUT",
    "FILE_REASON_TOO_LARGE",
    "FILE_REASON_UNAUTHORIZED",
    "FILE_REASON_UNSAFE_ROUTE",
    "FILE_TRANSFER_TIMEOUT_SECONDS",
    "MAX_DIALLED_ADDRESSES",
    "MAX_FILE_BYTES",
    "MAX_FILE_REDIRECTS",
    "REFUSAL_ADDRESS_REFUSED",
    "REFUSAL_HOST_NOT_ALLOWED",
    "REFUSAL_HOST_UNRESOLVED",
    "REFUSAL_TOO_MANY_REDIRECTS",
    "SLACK_FILE_HOST",
    "UNSAFE_PATH_CHARS_RE",
    "SlackAttachmentError",
    "SlackFileTransferRefused",
    "address_refusal_reason",
    "assert_slack_file_url",
    "describe_downloaded_attachments",
    "describe_event_attachments",
    "download_attachment",
    "event_entries",
    "fetch_attachment_bytes",
    "https_file_host",
    "normalised_hosts",
    "private_url",
    "resolve_attachment",
    "resolve_validated_addresses",
    "safe_path_component",
    "slack_file_client",
    "slack_file_name",
    "slack_file_transport",
    "stream_slack_file",
    "unique_path",
    "unreadable_file_notice",
    "upload_file",
    "uploaded_file_id",
]
