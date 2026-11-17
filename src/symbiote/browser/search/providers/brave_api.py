"""Brave Search, called directly with the host's own Brave key.

No gateway in between::

    register(
        kernel,
        search_backend="brave_api",
        search_options={"provider_options": {"brave_api": {"api_key": "BSA..."}}},
    )

The key comes from ``provider_options["brave_api"]["api_key"]`` or from the
``BRAVE_API_KEY`` environment variable. ``base_url`` (same place) replaces
``https://api.search.brave.com`` for a compatible endpoint.

Request: ``GET {base_url}/res/v1/web/search?q=...&count=N`` with the key in
``X-Subscription-Token``. The web results are normalized to
``{url, title, snippet}``.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from symbiote.browser.config import SearchOptions

_DEFAULT_BASE_URL = "https://api.search.brave.com"


class BraveSearchAPI:
    """SearchProvider over the Brave Search API."""

    name = "brave_api"

    def __init__(self, options: SearchOptions) -> None:
        own = options.options_for(self.name)
        self._timeout = options.timeout_seconds
        self._key = str(own.get("api_key") or os.getenv("BRAVE_API_KEY", ""))
        if not self._key:
            raise ValueError(
                "Brave API key not set. Provide "
                'search_options.provider_options["brave_api"]["api_key"] '
                "or set BRAVE_API_KEY in env."
            )
        base = str(own.get("base_url") or _DEFAULT_BASE_URL).rstrip("/")
        self._endpoint = f"{base}/res/v1/web/search"

    async def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        limit = max(1, min(20, int(limit)))
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                self._endpoint,
                params={"q": query, "count": str(limit)},
                headers={"X-Subscription-Token": self._key, "Accept": "application/json"},
            )
            response.raise_for_status()
            body = response.json()
        items = (body.get("web") or {}).get("results") or []
        return [
            {
                "url": item.get("url", ""),
                "title": item.get("title", ""),
                "snippet": item.get("description", ""),
            }
            for item in items[:limit]
        ]
