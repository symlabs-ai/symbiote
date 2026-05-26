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

from typing import Any

from symbiote.browser.search.providers.base import (
    CrawlProvider,
    ExtractProvider,
    SearchProvider,
)
from symbiote.environment.descriptors import ToolDescriptor

WEB_SEARCH_DESCRIPTOR = ToolDescriptor(
    tool_id="web_search",
    name="Web Search",
    description=(
        "Search the web for current information. Returns a list of results "
        "with URL, title, and a short snippet of the page content. Use this "
        "to find up-to-date facts, news, documentation, or to discover URLs "
        "you may then read in detail via browser_navigate / browser_snapshot."
    ),
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


def build_handlers(
    search: SearchProvider | None = None,
    extract: ExtractProvider | None = None,
    crawl: CrawlProvider | None = None,
) -> dict[str, Any]:
    """Build async handlers for whichever providers were supplied.

    Args:
        search: SearchProvider; enables `web_search` when given.
        extract: ExtractProvider; enables `web_extract` when given.
        crawl: CrawlProvider; enables `web_crawl` when given.

    Returns:
        {tool_id: async-handler} mapping containing only the tools whose
        backing provider was passed. Callers iterate this map and register
        the descriptor + handler together on the ToolGateway.
    """
    handlers: dict[str, Any] = {}

    if search is not None:

        async def web_search(params: dict[str, Any]) -> dict[str, Any]:
            query = params["query"]
            limit = int(params.get("limit", 5))
            results = await search.search(query, limit=limit)
            return {"results": results, "count": len(results)}

        handlers[WEB_SEARCH_DESCRIPTOR.tool_id] = web_search

    if extract is not None:

        async def web_extract(params: dict[str, Any]) -> dict[str, Any]:
            from symbiote.security.network import validate_url

            url = params["url"]
            validate_url(url)  # SSRF guard before any HTTP I/O
            return await extract.extract(url)

        handlers[WEB_EXTRACT_DESCRIPTOR.tool_id] = web_extract

    if crawl is not None:

        async def web_crawl(params: dict[str, Any]) -> dict[str, Any]:
            from symbiote.security.network import validate_url

            domain = params["domain"]
            validate_url(domain)
            instruction = params["instruction"]
            max_pages = int(params.get("max_pages", 10))
            pages = await crawl.crawl(
                domain, instruction=instruction, max_pages=max_pages
            )
            return {"pages": pages, "count": len(pages)}

        handlers[WEB_CRAWL_DESCRIPTOR.tool_id] = web_crawl

    return handlers
