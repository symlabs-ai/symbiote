"""``http`` — read a public page with no service in between.

One GET through the fetcher (``symbiote.browser.search.fetch``: the host's
own, or the package's plain client), then the content by its type::

    register(kernel, extract_backend="http")
    register(kernel, extract_backend=["http", "firecrawl"])   # with a reserve

- The request asks for ``text/markdown`` first: a site that serves a markdown
  version of the page answers with it, and it is returned as it is.
- HTML becomes light markdown (headings, lists, links), without navigation,
  footers, forms and scripts. Nothing runs: a page built with JavaScript
  yields little or no text, and an empty content lets the next provider of a
  chain try.
- Plain text and JSON are returned as they are. Anything else (PDF, images,
  archives) raises, so a chain moves on.

A long page is cut in the middle, keeping its beginning and its end, with a
line saying so (``max_content_chars``); the body is read up to
``max_page_bytes``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from symbiote.browser.config import SearchOptions
from symbiote.browser.search.fetch import (
    FetchedPage,
    FetchError,
    fetch_page,
    resolve_fetcher,
)
from symbiote.browser.search.html_text import page_text, page_title, parse_html

ACCEPT = "text/markdown, text/html;q=0.9, text/plain;q=0.8, application/json;q=0.7, */*;q=0.1"
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_MARKDOWN_TYPES = frozenset({"text/markdown", "text/x-markdown"})
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w.:-]+)""", re.IGNORECASE)
_MD_HEADING = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_SIZE_NOTE = "\n\n[The page is larger than the size limit; it was cut here.]"


def _charset(page: FetchedPage) -> str:
    match = re.search(r"charset\s*=\s*\"?([\w.:-]+)", page.header("content-type"), re.IGNORECASE)
    if match:
        return match.group(1)
    meta = _META_CHARSET.search(page.body[:2048])
    return meta.group(1).decode("ascii", "replace") if meta else "utf-8"


def _decode(page: FetchedPage) -> str:
    try:
        return page.body.decode(_charset(page), errors="replace")
    except LookupError:  # a charset Python does not know
        return page.body.decode("utf-8", errors="replace")


def clip_middle(text: str, limit: int) -> tuple[str, bool]:
    """``text`` within ``limit`` characters: when longer, its beginning and
    its end with a line between them saying how much was left out."""
    if limit <= 0 or len(text) <= limit:
        return text, False
    head = text[: int(limit * 0.75)]
    tail = text[len(text) - (limit - len(head)) :]
    omitted = len(text) - len(head) - len(tail)
    note = (
        f"\n\n[... {omitted} characters left out of the middle of this page; "
        "what follows is its end ...]\n\n"
    )
    return head + note + tail, True


class HttpExtract:
    """ExtractProvider that reads the page itself, through a fetcher."""

    name = "http"

    def __init__(self, options: SearchOptions | None = None, *, fetcher: Any = None) -> None:
        self._options = options or SearchOptions()
        self._fetcher = fetcher if fetcher is not None else resolve_fetcher(self._options)

    async def extract(self, url: str) -> dict[str, Any]:
        own = self._options.options_for(self.name)
        headers = {"Accept": ACCEPT}
        if own.get("accept_language"):
            headers["Accept-Language"] = str(own["accept_language"])
        page = await fetch_page(
            self._fetcher,
            url,
            headers=headers,
            timeout=self._options.timeout_seconds,
            max_bytes=self._options.max_page_bytes,
        )
        if page.status >= 400:
            raise FetchError(f"HTTP {page.status} from {page.url}")
        content_type = page.header("content-type").split(";")[0].strip().lower()
        title, content = self._read(page, content_type)
        total = len(content)
        content, clipped = clip_middle(content, self._options.max_content_chars)
        if page.truncated:
            content += _SIZE_NOTE
        result: dict[str, Any] = {
            "url": page.url,
            "title": title,
            "content": content,
            "language": None,
            "platform": None,
            "metadata": {"content_type": content_type, "status": page.status},
            "content_quality": None,
            "quality_reason": None,
        }
        if clipped or page.truncated:
            result["truncated"] = True
            result["total_chars"] = total
        return result

    @staticmethod
    def _read(page: FetchedPage, content_type: str) -> tuple[str, str]:
        text = _decode(page)
        if content_type in _MARKDOWN_TYPES:
            heading = _MD_HEADING.search(text)
            return (heading.group(1).strip() if heading else ""), text.strip()
        looks_html = not content_type and text.lstrip()[:200].lower().startswith(("<!doctype html", "<html"))
        if content_type in _HTML_TYPES or looks_html:
            root = parse_html(text)
            return page_title(root), page_text(root, base_url=page.url)
        if content_type == "application/json" or content_type.endswith("+json"):
            try:
                return "", json.dumps(json.loads(text), ensure_ascii=False, indent=2)
            except ValueError:
                return "", text.strip()
        if content_type.startswith("text/") or content_type in ("application/xml", ""):
            return "", text.strip()
        raise ValueError(f"cannot read a page of type {content_type!r}: {page.url}")
