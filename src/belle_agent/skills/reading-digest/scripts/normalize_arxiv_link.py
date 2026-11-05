#!/usr/bin/env python3
"""Normalize arXiv links to abstract URLs for chat previews."""
from __future__ import annotations

import re
import sys
from urllib.parse import urlsplit, urlunsplit

_ARXIV_HOSTS = (
    "arxiv.org",
    "export.arxiv.org",
    "www.arxiv.org",
)

_PDF_RE = re.compile(r"^/(?:pdf|abs)/([^/?#]+?)(?:\.pdf)?$", re.IGNORECASE)


def normalize(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.hostname and parts.hostname.lower() in _ARXIV_HOSTS:
        m = _PDF_RE.match(parts.path or "")
        if m:
            paper_id = m.group(1)
            return urlunsplit(("https", "arxiv.org", f"/abs/{paper_id}", "", ""))
    return url


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: normalize_arxiv_link.py <url>", file=sys.stderr)
        return 2
    for arg in sys.argv[1:]:
        print(normalize(arg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
