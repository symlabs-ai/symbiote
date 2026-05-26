"""DuckDuckGo HTML provider — free, no credentials, lower quality.

Scrapes the static HTML version of DuckDuckGo at ``html.duckduckgo.com/html/``.
This is the same path used by claw-code's WebSearch tool: zero cost, zero
quota, but inherently fragile — if DDG changes their HTML structure, the
parser breaks. Use as a development fallback or for environments without
SymGateway access; prefer Brave-via-SymGateway in production.

Endpoint:
    GET https://html.duckduckgo.com/html/?q=<query>

We parse anchors with class ``result__a`` (DDG's stable structural marker for
result links) and decode the ``uddg`` query parameter from their redirect URL
to get the real destination. If no class-matched results come back (e.g. DDG
changed their template), we fall back to extracting *any* HTTP anchor with
text content — matches claw-code's defensive two-pass strategy.

Snippets are NOT returned today (the HTML structure is more brittle for those);
we may add them later in Phase 7+. Each hit has {url, title, snippet=""}.
"""

from __future__ import annotations

import html
import html.parser
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from symbiote.browser.config import SearchOptions

logger = logging.getLogger(__name__)

_BASE_URL = "https://html.duckduckgo.com/html/"
_USER_AGENT = "symbiote-browser/0.6 (+https://symlabs.ai; like claw-rust-tools/0.1)"


class DuckDuckGoHtmlProvider:
    """SearchProvider that scrapes DDG's static HTML interface."""

    name = "duckduckgo"

    def __init__(self, options: SearchOptions | None = None) -> None:
        self._options = options or SearchOptions()

    async def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        limit = max(1, min(20, int(limit)))
        params = {"q": query}
        async with httpx.AsyncClient(
            timeout=self._options.timeout_seconds,
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html"},
            follow_redirects=True,
        ) as client:
            response = await client.get(_BASE_URL, params=params)
            response.raise_for_status()
            body = response.text

        hits = _extract_result_anchors(body)
        if not hits:
            # Fallback: DDG layout may have shifted — pick any HTTP anchor with text.
            logger.info("DDG: no class=result__a anchors, falling back to generic links")
            hits = _extract_generic_anchors(body)

        # Dedupe by URL, preserve order, truncate.
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for hit in hits:
            url = hit["url"]
            if url in seen:
                continue
            seen.add(url)
            deduped.append(hit)
            if len(deduped) >= limit:
                break
        logger.info(
            "duckduckgo query=%r returned=%d (limit=%d)", query[:60], len(deduped), limit
        )
        return deduped


# ── HTML parsing helpers ───────────────────────────────────────────────────


def _extract_result_anchors(body: str) -> list[dict[str, Any]]:
    """Pull <a class="result__a" href="..."> items out of the DDG HTML page."""
    parser = _AnchorParser(class_filter="result__a")
    parser.feed(body)
    return [
        {"url": decoded, "title": title, "snippet": ""}
        for href, title in parser.anchors
        for decoded in [_decode_uddg_redirect(href)]
        if decoded and (decoded.startswith("http://") or decoded.startswith("https://"))
    ]


def _extract_generic_anchors(body: str) -> list[dict[str, Any]]:
    """Fallback: any <a href="http..."> with non-empty text content."""
    parser = _AnchorParser(class_filter=None)
    parser.feed(body)
    out: list[dict[str, Any]] = []
    for href, title in parser.anchors:
        decoded = _decode_uddg_redirect(href) or href
        if not (decoded.startswith("http://") or decoded.startswith("https://")):
            continue
        if not title.strip():
            continue
        out.append({"url": decoded, "title": title, "snippet": ""})
    return out


_UDDG_DECODE_PATH = re.compile(r"^/+l/?$")


def _decode_uddg_redirect(url: str | None) -> str | None:
    """Resolve DDG's ``/l/?uddg=<encoded_url>`` redirect to the real destination.

    Returns *url* unchanged when it's already absolute and not a DDG redirect.
    Returns None when the input is empty or malformed.
    """
    if not url:
        return None

    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = "https://duckduckgo.com" + url

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.endswith("duckduckgo.com") and _UDDG_DECODE_PATH.match(parsed.path or ""):
        qs = parse_qs(parsed.query)
        target = qs.get("uddg") or qs.get("u")
        if target:
            return html.unescape(target[0])
        return None

    return url


class _AnchorParser(html.parser.HTMLParser):
    """Lightweight ``<a>`` extractor with optional CSS class filter.

    Collects (href, text) tuples. We avoid pulling in BeautifulSoup so the
    DDG provider remains zero-dep beyond stdlib + httpx (already present).
    """

    def __init__(self, *, class_filter: str | None) -> None:
        super().__init__(convert_charrefs=True)
        self._class_filter = class_filter
        self._depth = 0
        self._current_href: str | None = None
        self._current_text: list[str] = []
        self.anchors: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attr_map = dict(attrs)
        classes = (attr_map.get("class") or "").split()
        if self._class_filter is not None and self._class_filter not in classes:
            return
        href = attr_map.get("href")
        if not href:
            return
        # Nested <a> inside <a> would be invalid HTML, but be defensive.
        if self._current_href is None:
            self._current_href = href
            self._current_text = []
            self._depth = 1
        else:
            self._depth += 1

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._current_href is None:
            return
        self._depth -= 1
        if self._depth > 0:
            return
        text = " ".join("".join(self._current_text).split()).strip()
        self.anchors.append((self._current_href, text))
        self._current_href = None
        self._current_text = []
