"""Web search through a SearXNG instance the host runs or trusts.

SearXNG is a self-hosted metasearch engine: no key, no gateway::

    register(
        kernel,
        search_backend="searxng",
        search_options={"provider_options": {"searxng": {"base_url": "http://localhost:8888"}}},
    )

The address comes from ``provider_options["searxng"]["base_url"]`` or from the
``SEARXNG_URL`` environment variable. The instance must have the JSON output
format enabled (``search.formats: [html, json]`` in its settings).

Request: ``GET {base_url}/search?q=...&format=json``. The results are
normalized to ``{url, title, snippet}``.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from symbiote.browser.config import SearchOptions


class SearxngSearch:
    """SearchProvider over a SearXNG instance."""

    name = "searxng"

    def __init__(self, options: SearchOptions) -> None:
        own = options.options_for(self.name)
        self._timeout = options.timeout_seconds
        base = str(own.get("base_url") or os.getenv("SEARXNG_URL", "")).rstrip("/")
        if not base:
            raise ValueError(
                "SearXNG address not set. Provide "
                'search_options.provider_options["searxng"]["base_url"] '
                "or set SEARXNG_URL in env."
            )
        self._endpoint = f"{base}/search"

    async def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        limit = max(1, min(20, int(limit)))
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                self._endpoint,
                params={"q": query, "format": "json"},
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            body = response.json()
        items = body.get("results") or []
        return [
            {
                "url": item.get("url", ""),
                "title": item.get("title", ""),
                "snippet": item.get("content", ""),
            }
            for item in items[:limit]
        ]
