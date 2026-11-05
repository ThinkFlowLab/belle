"""Fetch public PDFs or HTML, preferring a linked citation PDF."""

import asyncio
import ipaddress
import logging
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from belle_agent.second_brain.failures import describe

logger = logging.getLogger(__name__)

_SLACK_LINK_RE = re.compile(r"<(https?://[^>|\s]+)(?:\|[^>]*)?>", re.IGNORECASE)
_BARE_URL_RE = re.compile(r"(?<![<|])\bhttps?://[^\s<>|]+", re.IGNORECASE)

_TRAILING_PUNCTUATION = ".,;:!?)]}'\"" + "’”"

_PDF_TYPES = frozenset({"application/pdf", "application/x-pdf"})
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})

#: Citation tags on paper landing pages.
_CITATION_PDF_META = "citation_pdf_url"
_CITATION_TITLE_META = "citation_title"
_HEAD_END_RE = re.compile(r"</head\s*>", re.IGNORECASE)

_DEFAULT_MIN_CHARS = 600
_DEFAULT_MAX_CHARS = 400_000
_DEFAULT_MAX_BYTES = 25 * 1024 * 1024
_DEFAULT_TIMEOUT_SECONDS = 20.0

# Validate every redirect target before connecting.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 5
# Validate every DNS answer, then cap the number of connection attempts.
_MAX_DIALLED_ADDRESSES = 4


@dataclass
class LinkFetch:
    """Fetched PDF or HTML text selected for ingestion."""

    url: str
    kind: str  # "pdf" | "html"
    title: str = ""
    content: bytes = b""  # a PDF's raw bytes, written to ``sources/``
    text: str = ""  # an HTML page's extracted main text; the page itself is not kept
    filename: str = ""
    mime_type: str = ""


def extract_urls(text: str) -> list[str]:
    """Return unique HTTP(S) URLs in message order, unwrapping Slack links."""
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()

    def _keep(raw: str) -> None:
        url = raw.strip().rstrip(_TRAILING_PUNCTUATION)
        if url and url not in seen:
            seen.add(url)
            found.append(url)

    for match in _SLACK_LINK_RE.finditer(text):
        _keep(match.group(1))
    for match in _BARE_URL_RE.finditer(_SLACK_LINK_RE.sub(" ", text)):
        _keep(match.group(0))
    return found


def is_denied_host(url: str, deny_hosts: list[str] | tuple[str, ...] | None) -> bool:
    """Match denied hosts and subdomains on label boundaries."""
    if not deny_hosts:
        return False
    host = (urlparse(url).hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    for entry in deny_hosts:
        denied = str(entry or "").strip().lower().lstrip("*.").rstrip(".")
        if denied and (host == denied or host.endswith(f".{denied}")):
            return True
    return False


def _link_origin(url: str) -> tuple[str, int]:
    """Return the HTTP(S) host and port, rejecting malformed URL origins."""
    raw = str(url or "").strip()
    if not raw:
        raise ValueError("url is empty")
    try:
        parsed = httpx.URL(raw)
    except httpx.InvalidURL as exc:
        raise ValueError("invalid link URL") from exc
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported url scheme {parsed.scheme!r}; only http(s)")
    if not parsed.host:
        raise ValueError("url has no hostname")
    if parsed.username or parsed.password:
        raise ValueError("link URL cannot contain credentials")
    return parsed.raw_host.decode("ascii"), parsed.port or (443 if parsed.scheme == "https" else 80)


def _resolve_public_addresses(host: str, port: int) -> tuple[str, ...]:
    """Validate every DNS answer before returning addresses to dial."""
    try:
        answers = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"link target {host} cannot resolve") from exc
    addresses: list[str] = []
    for answer in answers:
        raw = str(answer[4][0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise ValueError(f"link target {host} resolved to an invalid address") from exc
        public_address = getattr(address, "ipv4_mapped", None) or address
        if not public_address.is_global:
            raise ValueError(f"link target {host} has a non-public address")
        if raw not in addresses:
            addresses.append(raw)
    if not addresses:
        raise ValueError(f"link target {host} cannot resolve")
    return tuple(addresses[:_MAX_DIALLED_ADDRESSES])


class _PinnedLinkBackend:
    """Dial validated IPs while httpcore retains the URL's Host and TLS SNI."""

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
        addresses = await asyncio.to_thread(_resolve_public_addresses, host, port)
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
        if last is not None:
            raise last
        raise ValueError("link target has no address to dial")

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> Any:
        raise ValueError("link fetch cannot use a Unix socket")


class _PublicLinkTransport(httpx.AsyncHTTPTransport):
    def __init__(self) -> None:
        super().__init__()
        pool = getattr(self, "_pool", None)
        backend = getattr(pool, "_network_backend", None)
        if backend is None:
            raise RuntimeError("httpx does not expose a network backend to pin")
        pool._network_backend = _PinnedLinkBackend(backend)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        _link_origin(str(request.url))
        return await super().handle_async_request(request)


def _public_link_transport() -> httpx.AsyncBaseTransport:
    return _PublicLinkTransport()


async def check_url_allowed_for_fetch(url: str) -> None:
    """Check a URL against the same address policy used by the fetch transport."""
    hostname, port = _link_origin(url)
    await asyncio.to_thread(_resolve_public_addresses, hostname, port)


async def fetch_link(
    url: str,
    *,
    cfg: dict[str, Any] | None = None,
    extract_html: Callable[[str], tuple[str, str]] | None = None,
) -> LinkFetch | None:
    """Fetch a document, return None for a skipped link, or raise on failure."""
    settings = cfg or {}
    deny_hosts = settings.get("deny_hosts") or []
    min_chars = int(settings.get("min_chars") or _DEFAULT_MIN_CHARS)
    max_chars = int(settings.get("max_chars") or _DEFAULT_MAX_CHARS)
    max_bytes = int(settings.get("max_bytes") or _DEFAULT_MAX_BYTES)
    timeout_seconds = float(settings.get("fetch_timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds),
        transport=_public_link_transport(),
        trust_env=False,
    ) as http:
        got = await _get(http, url, deny_hosts=deny_hosts, max_bytes=max_bytes)
        if got is None:
            return None
        final_url, content_type, body = got
        if content_type in _PDF_TYPES:
            return _pdf_fetch(final_url, body)
        html = body.decode("utf-8", errors="replace")
        paper = await _citation_pdf(http, html, final_url, deny_hosts=deny_hosts, max_bytes=max_bytes)
        if paper is not None:
            return paper

    title, text = extract_html(html) if extract_html else ("", "")
    text = text[:max_chars]
    if len(text) < min_chars:
        logger.info(
            "[SecondBrain] link %s declined: %d chars of main text, floor is %d",
            final_url,
            len(text),
            min_chars,
        )
        return None
    return LinkFetch(
        url=final_url,
        kind="html",
        title=title or final_url,
        text=text,
        filename=_filename_from_url(final_url, ".txt"),
        mime_type="text/html",
    )


async def _get(
    http: httpx.AsyncClient,
    url: str,
    *,
    deny_hosts: list[str] | tuple[str, ...],
    max_bytes: int,
) -> tuple[str, str, bytes] | None:
    """``(final url, content type, body)`` for a PDF or an HTML page, or
    ``None`` for any other media type and for HTML from a deny-listed host."""
    final_url = url
    for _ in range(_MAX_REDIRECTS + 1):
        _link_origin(final_url)
        # The transport resolves, validates, and dials each hop's address.
        # Redirects stay manual so no Location bypasses URL validation.
        async with http.stream("GET", final_url, follow_redirects=False) as response:
            if response.status_code in _REDIRECT_STATUSES:
                location = str(response.headers.get("location") or "").strip()
                if not location:
                    raise ValueError(f"HTTP {response.status_code} with no Location from {final_url}")
                final_url = urljoin(final_url, location)
                continue
            if response.status_code != 200:
                raise ValueError(f"HTTP {response.status_code} fetching {final_url}")

            content_type = str(response.headers.get("content-type") or "").split(";")[0].strip().lower()
            is_pdf = content_type in _PDF_TYPES
            if not is_pdf and content_type not in _HTML_TYPES:
                logger.info(
                    "[SecondBrain] link %s declined: content-type %s",
                    final_url,
                    content_type or "unknown",
                )
                return None
            # HTML only, against the host that served it: a PDF is a
            # document whoever hosts it.
            if not is_pdf and is_denied_host(final_url, deny_hosts):
                logger.info("[SecondBrain] link %s declined: deny-listed host", final_url)
                return None

            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ValueError(f"{final_url} exceeded {max_bytes} bytes")
            return final_url, content_type, bytes(body)
    raise ValueError(f"more than {_MAX_REDIRECTS} redirects following {url}")


def _pdf_fetch(url: str, body: bytes, title: str = "") -> LinkFetch:
    name = _filename_from_url(url, ".pdf")
    return LinkFetch(
        url=url,
        kind="pdf",
        title=title or name,
        content=body,
        filename=name,
        mime_type="application/pdf",
    )


async def _citation_pdf(
    http: httpx.AsyncClient,
    html: str,
    page_url: str,
    *,
    deny_hosts: list[str] | tuple[str, ...],
    max_bytes: int,
) -> LinkFetch | None:
    """Fetch the cited PDF under the same URL checks, or keep the page."""
    meta = _citation_meta(html)
    target = meta.get(_CITATION_PDF_META, "")
    if not target:
        return None
    pdf_url = target
    try:
        pdf_url = urljoin(page_url, target)
        if pdf_url == page_url:
            return None
        got = await _get(http, pdf_url, deny_hosts=deny_hosts, max_bytes=max_bytes)
    except (ValueError, httpx.HTTPError) as exc:
        logger.info(
            "[SecondBrain] citation PDF %s for %s not fetched (%s); keeping the page",
            pdf_url,
            page_url,
            exc,
        )
        return None
    if got is None or got[1] not in _PDF_TYPES:
        logger.info(
            "[SecondBrain] citation PDF %s for %s is not a PDF; keeping the page",
            pdf_url,
            page_url,
        )
        return None
    final_url, _, body = got
    return _pdf_fetch(final_url, body, meta.get(_CITATION_TITLE_META, ""))


class _CitationMetaParser(HTMLParser):
    """The first ``citation_pdf_url`` and ``citation_title`` a page declares."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        values = dict(attrs)
        name = str(values.get("name") or "").strip().lower()
        content = str(values.get("content") or "").strip()
        if name in (_CITATION_PDF_META, _CITATION_TITLE_META) and content:
            self.found.setdefault(name, content)


def _citation_meta(html: str) -> dict[str, str]:
    """Read citation tags from the page head."""
    head_end = _HEAD_END_RE.search(html)
    parser = _CitationMetaParser()
    try:
        parser.feed(html[: head_end.start()] if head_end else html)
        parser.close()
    except Exception as exc:  # noqa: BLE001
        logger.info("[SecondBrain] citation meta unreadable: %s", describe(exc))
    return parser.found


def _filename_from_url(url: str, default_suffix: str) -> str:
    """Get a readable name for the corpus to sanitize before writing."""
    parsed = urlparse(url)
    tail = (parsed.path or "").rstrip("/").rsplit("/", 1)[-1]
    if not tail:
        tail = (parsed.hostname or "link").replace(".", "-")
    if "." not in tail:
        tail = f"{tail}{default_suffix}"
    return tail[:180]


__all__ = [
    "LinkFetch",
    "check_url_allowed_for_fetch",
    "extract_urls",
    "fetch_link",
    "is_denied_host",
]
