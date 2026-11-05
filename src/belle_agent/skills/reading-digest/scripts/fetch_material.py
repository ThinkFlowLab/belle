#!/usr/bin/env python3
"""Safely fetch public HTML or PDF material and emit extracted JSON."""

from __future__ import annotations

import argparse
import hashlib
import io
import ipaddress
import json
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

_USER_AGENT = "belle-agent-reading-digest/1.0"
_IGNORED_TAGS = {"script", "style", "svg", "noscript"}


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")


def _validate_public_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("only public http/https URLs are supported")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(
            "URL must contain a public hostname and no embedded credentials"
        )
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = socket.getaddrinfo(
            parsed.hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise ValueError(f"hostname could not be resolved: {parsed.hostname}") from exc
    if not addresses:
        raise ValueError("hostname did not resolve to an address")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError(f"refusing non-public address: {ip}")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        _validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.description = ""
        self.text_parts: list[str] = []
        self._ignored_depth = 0
        self._in_title = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        tag = tag.lower()
        if tag in _IGNORED_TAGS:
            self._ignored_depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            values = {key.lower(): (value or "") for key, value in attrs}
            name = (values.get("name") or values.get("property") or "").lower()
            if name in {"description", "og:description", "twitter:description"} and not self.description:
                self.description = values.get("content", "").strip()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _IGNORED_TAGS and self._ignored_depth:
            self._ignored_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if not text or self._ignored_depth:
            return
        if self._in_title:
            self.title_parts.append(text)
        self.text_parts.append(text)


def _arxiv_pdf_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname and parsed.hostname.lower().endswith("arxiv.org"):
        match = re.fullmatch(r"/abs/([^/?#]+)", parsed.path)
        if match:
            return urllib.parse.urlunparse(
                parsed._replace(
                    path=f"/pdf/{match.group(1)}.pdf", query="", fragment=""
                )
            )
    return url


def _fetch(url: str, max_bytes: int) -> tuple[bytes, str, str]:
    _validate_public_url(url)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.1",
            "User-Agent": _USER_AGENT,
        },
    )
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    try:
        with opener.open(request, timeout=30) as response:
            final_url = response.geturl()
            _validate_public_url(final_url)
            content_type = response.headers.get_content_type().lower()
            data = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"source returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"source request failed: {exc.reason}") from exc
    if len(data) > max_bytes:
        raise RuntimeError(f"source exceeds the {max_bytes}-byte download limit")
    return data, final_url, content_type


def _extract_html(data: bytes, max_chars: int) -> dict[str, Any]:
    text = data.decode("utf-8", errors="replace")
    parser = _TextExtractor()
    parser.feed(text)
    extracted = "\n".join(parser.text_parts)
    return {
        "title": " ".join(parser.title_parts).strip(),
        "description": parser.description,
        "text": extracted[:max_chars],
        "truncated": len(extracted) > max_chars,
    }


def _extract_pdf(data: bytes, max_chars: int) -> dict[str, Any]:
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError as exc:
            raise RuntimeError(
                "PDF extraction requires pypdf or PyPDF2 in this Python environment"
            ) from exc
    reader = PdfReader(io.BytesIO(data))
    parts: list[str] = []
    for index, page in enumerate(reader.pages, start=1):
        page_text = page.extract_text() or ""
        parts.append(f"[Page {index}]\n{page_text.strip()}")
    extracted = "\n\n".join(parts)
    title = ""
    if reader.metadata and reader.metadata.title:
        title = str(reader.metadata.title).strip()
    return {
        "title": title,
        "description": "",
        "page_count": len(reader.pages),
        "text": extracted[:max_chars],
        "truncated": len(extracted) > max_chars,
    }


def _self_test() -> None:
    parser = _TextExtractor()
    parser.feed(
        "<html><head><title>Test Paper</title>"
        '<meta name="description" content="Evidence"></head>'
        "<body><script>ignore()</script><main>Claim and result</main></body></html>"
    )
    assert " ".join(parser.title_parts) == "Test Paper"
    assert parser.description == "Evidence"
    assert "Claim and result" in parser.text_parts
    assert all("ignore" not in part for part in parser.text_parts)
    try:
        _validate_public_url("http://127.0.0.1/private")
    except ValueError:
        pass
    else:
        raise AssertionError("loopback URL was not rejected")
    assert _arxiv_pdf_url("https://arxiv.org/abs/2401.00001") == (
        "https://arxiv.org/pdf/2401.00001.pdf"
    )
    print("self-test: ok")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url")
    parser.add_argument("--prefer-pdf", action="store_true")
    parser.add_argument("--max-bytes", type=int, default=20 * 1024 * 1024)
    parser.add_argument("--max-chars", type=int, default=160_000)
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.self_test:
        _self_test()
        return 0
    if not args.url:
        raise SystemExit("--url is required")
    if args.max_bytes <= 0 or args.max_chars <= 0:
        raise SystemExit("size limits must be positive")

    requested_url = args.url.strip()
    fetch_url = _arxiv_pdf_url(requested_url) if args.prefer_pdf else requested_url
    data, final_url, content_type = _fetch(fetch_url, args.max_bytes)
    is_pdf = content_type == "application/pdf" or data.startswith(b"%PDF-")
    extracted = (
        _extract_pdf(data, args.max_chars)
        if is_pdf
        else _extract_html(data, args.max_chars)
    )
    output = {
        "ok": True,
        "requested_url": requested_url,
        "retrieved_url": final_url,
        "retrieved_at_utc": datetime.now(UTC)
        .isoformat()
        .replace("+00:00", "Z"),
        "content_type": "application/pdf" if is_pdf else content_type,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        **extracted,
    }
    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    _configure_utf8_stdio()
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as exc:
        json.dump({"ok": False, "error": str(exc)}, sys.stderr, ensure_ascii=False)
        sys.stderr.write("\n")
        raise SystemExit(1) from exc
