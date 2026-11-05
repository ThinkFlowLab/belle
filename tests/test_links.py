"""URL extraction, the deny list, and the SSRF gate."""

import socket
import ssl
from functools import partial
from typing import Any, Self

import httpx
import pytest

from belle_agent.second_brain import links
from belle_agent.second_brain.documents import extract_html
from belle_agent.second_brain.links import (
    check_url_allowed_for_fetch,
    extract_urls,
    is_denied_host,
)
from belle_agent.second_brain.links import (
    fetch_link as _fetch_link,
)

fetch_link = partial(_fetch_link, extract_html=extract_html)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("<https://x.test/a>", ["https://x.test/a"]),
        ("<https://x.test/a|the paper>", ["https://x.test/a"]),
        ("see https://x.test/a.", ["https://x.test/a"]),
        ("(https://x.test/a)", ["https://x.test/a"]),
        ("https://x.test/a and https://y.test/b", ["https://x.test/a", "https://y.test/b"]),
        (
            "<https://b.test/2|b> then https://a.test/1",
            ["https://b.test/2", "https://a.test/1"],
        ),
        ("<https://x.test/a> again https://x.test/a", ["https://x.test/a"]),
        ("no links here", []),
        ("", []),
        ("ftp://x.test/a", []),
    ],
)
def test_extract_urls(text, expected) -> None:
    assert extract_urls(text) == expected


@pytest.mark.parametrize(
    "url,denied",
    [
        ("https://github.com/x/y", True),
        ("https://gist.github.com/x", True),
        ("https://GITHUB.COM/x", True),
        ("https://notgithub.com/x", False),
        ("https://github.com.evil.test/x", False),
        ("https://arxiv.org/abs/1", False),
    ],
)
def test_is_denied_host_matches_on_label_boundaries(url, denied) -> None:
    assert is_denied_host(url, ["github.com"]) is denied


def test_is_denied_host_with_no_list() -> None:
    assert is_denied_host("https://x.test/a", []) is False
    assert is_denied_host("https://x.test/a", None) is False


@pytest.mark.parametrize(
    "url",
    [
        "",
        "file:///etc/passwd",
        "gopher://x.test/",
        "https://",
    ],
)
@pytest.mark.asyncio
async def test_check_url_rejects_bad_schemes(url) -> None:
    with pytest.raises(ValueError):
        await check_url_allowed_for_fetch(url)


@pytest.mark.parametrize(
    "family,address",
    [
        (socket.AF_INET, "127.0.0.1"),
        (socket.AF_INET, "10.0.0.5"),
        (socket.AF_INET, "192.168.1.1"),
        (socket.AF_INET, "172.16.0.1"),
        # The cloud metadata endpoint. The hand-written range list in
        # skill_gen/url_ingest/url_safety.py misses this one entirely, which is
        # why is_global is used here instead of porting it.
        (socket.AF_INET, "169.254.169.254"),
        (socket.AF_INET6, "::1"),
        (socket.AF_INET6, "::ffff:10.0.0.5"),
    ],
)
@pytest.mark.asyncio
async def test_check_url_rejects_a_host_resolving_to_a_private_address(monkeypatch, family, address) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (
                family,
                None,
                None,
                "",
                (address, 0, 0, 0) if family == socket.AF_INET6 else (address, 0),
            )
        ],
    )
    with pytest.raises(ValueError, match="non-public"):
        await check_url_allowed_for_fetch("https://internal.test/x")


@pytest.mark.asyncio
async def test_check_url_rejects_when_any_address_is_private(monkeypatch) -> None:
    """A host with both a public and a private record is still refused."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET, None, None, "", ("93.184.216.34", 0)),
            (socket.AF_INET, None, None, "", ("127.0.0.1", 0)),
        ],
    )
    with pytest.raises(ValueError, match="non-public"):
        await check_url_allowed_for_fetch("https://mixed.test/x")


@pytest.mark.asyncio
async def test_check_url_accepts_a_public_address(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, None, None, "", ("93.184.216.34", 0))],
    )
    await check_url_allowed_for_fetch("https://example.test/x")


@pytest.mark.asyncio
async def test_check_url_rejects_an_unresolvable_host(monkeypatch) -> None:
    def _boom(*a, **kw):
        raise OSError("nope")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    with pytest.raises(ValueError, match="cannot resolve"):
        await check_url_allowed_for_fetch("https://nowhere.test/x")


@pytest.mark.asyncio
async def test_check_url_rejects_credentials() -> None:
    with pytest.raises(ValueError, match="credentials"):
        await check_url_allowed_for_fetch("https://user:pass@public.test/x")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:pass@public.test/x"])
@pytest.mark.asyncio
async def test_fetch_rejects_unsafe_url_before_dns(monkeypatch, url: str) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: pytest.fail("DNS was called"))
    with pytest.raises(ValueError, match="scheme|credentials"):
        await fetch_link(url)


def _mock_fetch(monkeypatch, handler) -> None:
    monkeypatch.setattr(links, "_public_link_transport", lambda: httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_pdf_is_accepted_even_from_a_denied_host(monkeypatch) -> None:
    """The deny list keeps issue trackers out; a PDF is a document anywhere."""

    def handler(request):
        return httpx.Response(200, content=b"%PDF-1.4 body", headers={"content-type": "application/pdf"})

    _mock_fetch(monkeypatch, handler)
    result = await fetch_link("https://github.com/x/paper.pdf", cfg={"deny_hosts": ["github.com"]})
    assert result is not None
    assert result.kind == "pdf"
    assert result.content.startswith(b"%PDF")


@pytest.mark.asyncio
async def test_denied_host_html_is_declined(monkeypatch) -> None:
    def handler(request):
        return httpx.Response(
            200,
            text="<html><body>" + "word " * 500 + "</body></html>",
            headers={"content-type": "text/html"},
        )

    _mock_fetch(monkeypatch, handler)
    assert await fetch_link("https://github.com/x/y", cfg={"deny_hosts": ["github.com"]}) is None


@pytest.mark.asyncio
async def test_thin_html_is_declined(monkeypatch) -> None:
    def handler(request):
        return httpx.Response(
            200,
            text="<html><body><p>too short</p></body></html>",
            headers={"content-type": "text/html"},
        )

    _mock_fetch(monkeypatch, handler)
    assert await fetch_link("https://x.test/a", cfg={"min_chars": 600}) is None


@pytest.mark.asyncio
async def test_unknown_content_type_is_declined(monkeypatch) -> None:
    def handler(request):
        return httpx.Response(200, content=b"\x00\x01", headers={"content-type": "video/mp4"})

    _mock_fetch(monkeypatch, handler)
    assert await fetch_link("https://x.test/v.mp4", cfg={}) is None


@pytest.mark.asyncio
async def test_oversized_body_raises(monkeypatch) -> None:
    def handler(request):
        return httpx.Response(200, content=b"%PDF" + b"x" * 5000, headers={"content-type": "application/pdf"})

    _mock_fetch(monkeypatch, handler)
    with pytest.raises(ValueError, match="exceeded"):
        await fetch_link("https://x.test/a.pdf", cfg={"max_bytes": 100})


@pytest.mark.asyncio
async def test_non_200_raises(monkeypatch) -> None:
    def handler(request):
        return httpx.Response(404, text="gone")

    _mock_fetch(monkeypatch, handler)
    with pytest.raises(ValueError, match="HTTP 404"):
        await fetch_link("https://x.test/a", cfg={})


_PAPER_BODY = "<p>" + "an abstract of the paper " * 60 + "</p>"


def _landing_page(pdf_url: str, title: str = "Attention Is All You Need") -> str:
    return (
        "<html><head><title>[1706.03762] the abstract page</title>"
        f'<meta name="citation_title" content="{title}">'
        f'<meta name="citation_pdf_url" content="{pdf_url}">'
        f"</head><body>{_PAPER_BODY}</body></html>"
    )


@pytest.mark.asyncio
async def test_a_landing_page_is_ingested_as_its_citation_pdf(monkeypatch) -> None:
    """An arXiv ``/abs/`` link is the paper, not the abstract page."""
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path.startswith("/abs/"):
            return httpx.Response(
                200,
                text=_landing_page("/pdf/1706.03762"),
                headers={"content-type": "text/html; charset=utf-8"},
            )
        return httpx.Response(200, content=b"%PDF-1.4 paper", headers={"content-type": "application/pdf"})

    _mock_fetch(monkeypatch, handler)
    result = await fetch_link("https://arxiv.test/abs/1706.03762", cfg={})

    assert result is not None and result.kind == "pdf"
    assert result.content == b"%PDF-1.4 paper"
    assert result.url == "https://arxiv.test/pdf/1706.03762"
    assert result.title == "Attention Is All You Need"
    assert seen == [
        "https://arxiv.test/abs/1706.03762",
        "https://arxiv.test/pdf/1706.03762",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pdf_response",
    [
        httpx.Response(404, text="gone"),
        # A paywall: the PDF link answers with a login page.
        httpx.Response(200, text="<html>sign in</html>", headers={"content-type": "text/html"}),
        httpx.Response(200, content=b"\x00", headers={"content-type": "video/mp4"}),
    ],
)
async def test_a_citation_pdf_that_is_not_a_pdf_keeps_the_page(monkeypatch, pdf_response) -> None:
    def handler(request):
        if request.url.path == "/paper":
            return httpx.Response(
                200,
                text=_landing_page("https://x.test/paper.pdf"),
                headers={"content-type": "text/html"},
            )
        return pdf_response

    _mock_fetch(monkeypatch, handler)
    result = await fetch_link("https://x.test/paper", cfg={"min_chars": 600})

    assert result is not None and result.kind == "html"
    assert result.url == "https://x.test/paper"
    assert "an abstract of the paper" in result.text


@pytest.mark.parametrize("citation_url", ["file:///etc/passwd", "http://["])
@pytest.mark.asyncio
async def test_an_invalid_citation_pdf_url_is_never_requested(monkeypatch, citation_url) -> None:
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(
            200,
            text=_landing_page(citation_url),
            headers={"content-type": "text/html"},
        )

    _mock_fetch(monkeypatch, handler)
    result = await fetch_link("https://x.test/paper", cfg={"min_chars": 600})

    assert result is not None and result.kind == "html"
    assert seen == ["https://x.test/paper"]


@pytest.mark.asyncio
async def test_a_page_without_citation_tags_is_ingested_as_before(monkeypatch) -> None:
    def handler(request):
        return httpx.Response(
            200,
            text=f"<html><head><title>a post</title></head><body>{_PAPER_BODY}</body></html>",
            headers={"content-type": "text/html"},
        )

    _mock_fetch(monkeypatch, handler)
    result = await fetch_link("https://x.test/post", cfg={"min_chars": 600})

    assert result is not None and result.kind == "html"


@pytest.mark.asyncio
async def test_a_denied_landing_page_is_not_followed(monkeypatch) -> None:
    """The deny list is checked on the page before its tags are read."""
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(
            200,
            text=_landing_page("https://pdfs.test/paper.pdf"),
            headers={"content-type": "text/html"},
        )

    _mock_fetch(monkeypatch, handler)
    assert await fetch_link("https://github.com/x/y", cfg={"deny_hosts": ["github.com"]}) is None
    assert seen == ["https://github.com/x/y"]


class _ResponseStream:
    def __init__(self, record: dict[str, Any], response: bytes) -> None:
        self.record = record
        self.response = response
        self.pending = b""

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> Self:
        self.record["server_hostname"] = server_hostname
        return self

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.record.setdefault("written", bytearray()).extend(buffer)
        self.pending = self.response

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        chunk, self.pending = self.pending[:max_bytes], self.pending[max_bytes:]
        return chunk

    async def aclose(self) -> None:
        pass

    def get_extra_info(self, info: str) -> None:
        return None


class _RecordingBackend:
    def __init__(self, record: dict[str, Any], response: bytes) -> None:
        self.record = record
        self.response = response

    async def connect_tcp(self, host: str, port: int, **kwargs: Any) -> _ResponseStream:
        self.record.setdefault("dialled", []).append((host, port))
        return _ResponseStream(self.record, self.response)


def _wire_transport(monkeypatch, response: bytes) -> dict[str, Any]:
    record: dict[str, Any] = {}
    transport = links._public_link_transport()
    transport._pool._network_backend._inner = _RecordingBackend(record, response)
    monkeypatch.setattr(links, "_public_link_transport", lambda: transport)
    return record


@pytest.mark.asyncio
async def test_fetch_dials_the_validated_address_once(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")

    def _resolve(host: str, *args: Any, **kwargs: Any) -> list[tuple]:
        calls.append(host)
        address = "93.184.216.34" if len(calls) == 1 else "169.254.169.254"
        return [(socket.AF_INET, None, None, "", (address, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _resolve)
    body = b"%PDF-1.4 body"
    response = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/pdf\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + body
    )
    record = _wire_transport(monkeypatch, response)

    result = await fetch_link("https://paper.test/file.pdf")

    assert result is not None and result.content == body
    assert calls == ["paper.test"]
    assert record["dialled"] == [("93.184.216.34", 443)]
    assert record["server_hostname"] == "paper.test"
    assert b"Host: paper.test\r\n" in record["written"]


@pytest.mark.asyncio
async def test_a_redirect_to_a_private_address_is_refused(monkeypatch) -> None:
    calls: list[str] = []

    def _resolve(host: str, *args: Any, **kwargs: Any) -> list[tuple]:
        calls.append(host)
        address = "169.254.169.254" if host == "internal.test" else "93.184.216.34"
        return [(socket.AF_INET, None, None, "", (address, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _resolve)
    response = (
        b"HTTP/1.1 302 Found\r\n"
        b"Location: http://internal.test/latest/meta-data/\r\n"
        b"Content-Length: 0\r\nConnection: close\r\n\r\n"
    )
    record = _wire_transport(monkeypatch, response)

    with pytest.raises(ValueError, match="non-public"):
        await fetch_link("https://public.test/paper")

    assert calls == ["public.test", "internal.test"]
    assert record["dialled"] == [("93.184.216.34", 443)]


@pytest.mark.asyncio
async def test_a_citation_pdf_on_a_private_address_is_refused(monkeypatch) -> None:
    """The PDF a page names passes the same address gate the page did."""
    calls: list[str] = []

    def _resolve(host: str, *args: Any, **kwargs: Any) -> list[tuple]:
        calls.append(host)
        address = "169.254.169.254" if host == "internal.test" else "93.184.216.34"
        return [(socket.AF_INET, None, None, "", (address, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _resolve)
    body = _landing_page("http://internal.test/latest/meta-data/").encode()
    response = (
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + body
    )
    record = _wire_transport(monkeypatch, response)

    result = await fetch_link("https://public.test/paper", cfg={"min_chars": 600})

    assert result is not None and result.kind == "html"
    assert calls == ["public.test", "internal.test"]
    assert record["dialled"] == [("93.184.216.34", 443)]


@pytest.mark.asyncio
async def test_a_redirect_to_a_public_address_is_followed(monkeypatch) -> None:
    """Redirects are ordinary -- a DOI or a shortener is one -- so they work."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, None, None, "", ("93.184.216.34", 0))],
    )

    def handler(request):
        if request.url.host == "doi.test":
            return httpx.Response(302, headers={"location": "https://publisher.test/paper.pdf"})
        return httpx.Response(200, content=b"%PDF-1.4 body", headers={"content-type": "application/pdf"})

    _mock_fetch(monkeypatch, handler)
    result = await fetch_link("https://doi.test/10.1/x", cfg={})
    assert result is not None and result.kind == "pdf"


@pytest.mark.asyncio
async def test_a_redirect_loop_gives_up(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, None, None, "", ("93.184.216.34", 0))],
    )

    def handler(request):
        return httpx.Response(302, headers={"location": "https://loop.test/again"})

    _mock_fetch(monkeypatch, handler)
    with pytest.raises(ValueError, match="[Rr]edirect"):
        await fetch_link("https://loop.test/start", cfg={})
