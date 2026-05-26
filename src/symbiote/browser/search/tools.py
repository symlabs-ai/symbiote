"""Web search tools — descriptors and handlers wired into ToolGateway.

The `web_search` tool returns a compact list of {url, title, snippet} suitable
for direct LLM consumption. Phase 4 will add `web_extract` and `web_crawl`.
"""

from __future__ import annotations

from typing import Any

from symbiote.browser.search.providers.base import SearchProvider
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


ALL_DESCRIPTORS = [WEB_SEARCH_DESCRIPTOR]


def build_handlers(provider: SearchProvider) -> dict[str, Any]:
    """Build async handlers bound to *provider* for registration on ToolGateway."""

    async def web_search(params: dict[str, Any]) -> dict[str, Any]:
        query = params["query"]
        limit = int(params.get("limit", 5))
        results = await provider.search(query, limit=limit)
        return {"results": results, "count": len(results)}

    return {WEB_SEARCH_DESCRIPTOR.tool_id: web_search}
