"""Web search/extract/crawl tools — descriptors and handlers wired into ToolGateway.

- `web_search` returns a compact list of {url, title, snippet}.
- `web_extract` returns the clean content of a single URL.
- `web_crawl` returns a list of pages matching a natural-language instruction.

Each tool maps to a different protocol on the provider side
(SearchProvider / ExtractProvider / CrawlProvider). `build_handlers` wires
only the tools whose backing provider is non-None — partial registration is
intentional so a host that only configured `web_search` doesn't expose stub
tools that always 500.
"""

from __future__ import annotations

import time
from typing import Any

from symbiote.browser.search.providers.base import (
    CrawlProvider,
    ExtractProvider,
    SearchProvider,
)
from symbiote.environment.descriptors import ToolDescriptor

_SEARCH_DESCRIPTION = (
    "Search the web for current information. Returns a list of results "
    "with URL, title, and a short snippet of the page content. Use this "
    "to find up-to-date facts, news, documentation, or to discover URLs"
)
_SEARCH_OUTCOMES = (
    " An empty list with outcome \"no_results\" means the search ran and found "
    "nothing. An error means the search did not run, which says nothing about "
    "whether results exist."
)

WEB_SEARCH_DESCRIPTOR = ToolDescriptor(
    tool_id="web_search",
    name="Web Search",
    description=_SEARCH_DESCRIPTION + "." + _SEARCH_OUTCOMES,
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language search query.",
            },
            "limit": {
                "type": "integer",
                "description": "Max results to return (1-20).",
                "default": 5,
                "minimum": 1,
                "maximum": 20,
            },
        },
        "required": ["query"],
    },
    handler_type="builtin",
    risk_level="low",
    tags=["web", "search"],
)

WEB_EXTRACT_DESCRIPTOR = ToolDescriptor(
    tool_id="web_extract",
    name="Web Extract",
    description=(
        "Fetch a single URL and return its clean main content as text/markdown, "
        "stripped of navigation/ads/boilerplate. Platform-aware: YouTube returns "
        "transcript, Reddit returns post + top comments, generic sites return "
        "the article body. Prefer this over browser_navigate when you just "
        "need to read a page, not interact with it."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Absolute URL to extract.",
            },
        },
        "required": ["url"],
    },
    handler_type="builtin",
    risk_level="low",
    tags=["web", "extract"],
)

WEB_CRAWL_DESCRIPTOR = ToolDescriptor(
    tool_id="web_crawl",
    name="Web Crawl",
    description=(
        "Crawl a domain following a natural-language instruction. Returns a "
        "list of pages (URL + title + content) the crawler judged relevant. "
        "Use when you need to gather many pages from one site — e.g. all "
        "product pages, all blog posts on a topic, etc."
    ),
    parameters={
        "type": "object",
        "properties": {
            "domain": {
                "type": "string",
                "description": "Domain or starting URL to crawl.",
            },
            "instruction": {
                "type": "string",
                "description": "What to look for / what kind of pages to keep.",
            },
            "max_pages": {
                "type": "integer",
                "description": "Cap on pages crawled (1-100).",
                "default": 10,
                "minimum": 1,
                "maximum": 100,
            },
        },
        "required": ["domain", "instruction"],
    },
    handler_type="builtin",
    risk_level="medium",
    tags=["web", "crawl"],
)


ALL_DESCRIPTORS = [
    WEB_SEARCH_DESCRIPTOR,
    WEB_EXTRACT_DESCRIPTOR,
    WEB_CRAWL_DESCRIPTOR,
]

_NO_RESULTS_NOTE = (
    "The search ran and found nothing for this query. Try other words or a "
    "shorter query."
)
_CACHE_MAX_ENTRIES = 256


def web_search_descriptor(readers: list[str] | None = None) -> ToolDescriptor:
    """The ``web_search`` descriptor, naming only the tools registered with
    it for reading a result in detail (``readers``: tool ids)."""
    if not readers:
        return WEB_SEARCH_DESCRIPTOR
    return WEB_SEARCH_DESCRIPTOR.model_copy(update={
        "description": (
            f"{_SEARCH_DESCRIPTION} you may then read in detail via "
            f"{' / '.join(readers)}.{_SEARCH_OUTCOMES}"
        )
    })


class _TtlCache:
    """Answers kept in memory for ``ttl`` seconds (``0``: nothing is kept)."""

    def __init__(self, ttl: float) -> None:
        self._ttl = ttl
        self._entries: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}

    def get(self, key: tuple[Any, ...]) -> dict[str, Any] | None:
        entry = self._entries.get(key) if self._ttl > 0 else None
        if entry is None:
            return None
        if time.monotonic() - entry[0] > self._ttl:
            del self._entries[key]
            return None
        return dict(entry[1])

    def put(self, key: tuple[Any, ...], value: dict[str, Any]) -> None:
        if self._ttl <= 0:
            return
        while len(self._entries) >= _CACHE_MAX_ENTRIES:
            del self._entries[next(iter(self._entries))]  # the oldest
        self._entries[key] = (time.monotonic(), dict(value))


def build_handlers(
    search: SearchProvider | None = None,
    extract: ExtractProvider | None = None,
    crawl: CrawlProvider | None = None,
    policy: Any = None,
    cache_ttl: float = 0.0,
) -> dict[str, Any]:
    """Build async handlers for whichever providers were supplied.

    Args:
        search: SearchProvider; enables `web_search` when given.
        extract: ExtractProvider; enables `web_extract` when given.
        crawl: CrawlProvider; enables `web_crawl` when given.
        policy: Optional WebsitePolicy checked before every URL is fetched
            (web_extract / web_crawl), as the browser tools already do.
        cache_ttl: Seconds an answer of web_search / web_extract is kept in
            memory and reused for the same query or address (0: no cache).

    Returns:
        {tool_id: async-handler} mapping containing only the tools whose
        backing provider was passed. Callers iterate this map and register
        the descriptor + handler together on the ToolGateway.
    """
    handlers: dict[str, Any] = {}
    cache = _TtlCache(cache_ttl)

    if search is not None:
        # A chain says which provider answered and what the others did.
        run = getattr(search, "run", None)

        async def web_search(params: dict[str, Any]) -> dict[str, Any]:
            query = params["query"]
            limit = int(params.get("limit", 5))
            cached = cache.get(("search", query, limit))
            if cached is not None:
                return cached
            extra: dict[str, Any] = {}
            if callable(run):
                ran = await run(query, limit=limit)
                results = ran.results
                if ran.source:
                    extra["source"] = ran.source
                if ran.attempts:
                    extra["attempts"] = ran.attempts
            else:
                results = await search.search(query, limit=limit)
            if not results:
                return {
                    "results": [],
                    "count": 0,
                    "outcome": "no_results",
                    "note": _NO_RESULTS_NOTE,
                    **extra,
                }
            answer = {"results": results, "count": len(results), "outcome": "results", **extra}
            cache.put(("search", query, limit), answer)
            return answer

        handlers[WEB_SEARCH_DESCRIPTOR.tool_id] = web_search

    if extract is not None:

        async def web_extract(params: dict[str, Any]) -> dict[str, Any]:
            from symbiote.security.network import validate_url

            url = params["url"]
            if policy is not None:
                policy.check(url)
            validate_url(url)  # SSRF guard before any HTTP I/O
            cached = cache.get(("extract", url))
            if cached is not None:
                return cached
            result = await extract.extract(url)
            final = result.get("url") if isinstance(result, dict) else None
            if policy is not None and isinstance(final, str) and final and final != url:
                policy.check(final)  # the page a redirect led to
            if isinstance(result, dict) and (result.get("content") or "").strip():
                cache.put(("extract", url), result)
            return result

        handlers[WEB_EXTRACT_DESCRIPTOR.tool_id] = web_extract

    if crawl is not None:

        async def web_crawl(params: dict[str, Any]) -> dict[str, Any]:
            from symbiote.security.network import validate_url

            domain = params["domain"]
            if "://" not in domain:
                domain = f"https://{domain}"  # the schema asks for a domain
            if policy is not None:
                policy.check(domain)
            validate_url(domain)
            instruction = params["instruction"]
            max_pages = int(params.get("max_pages", 10))
            pages = await crawl.crawl(
                domain, instruction=instruction, max_pages=max_pages
            )
            return {"pages": pages, "count": len(pages)}

        handlers[WEB_CRAWL_DESCRIPTOR.tool_id] = web_crawl

    return handlers
