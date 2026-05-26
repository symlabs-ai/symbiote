"""Firecrawl via SymGateway proxy.

Implements both ExtractProvider (single-URL clean markdown) and CrawlProvider
(instruction-driven domain crawl). Routes through SymGateway's proxy layer
the same way the Brave search provider does, so we never hold the Firecrawl
API key directly — DevOps controls it via SymVault, billing rolls up through
the gateway.

Endpoints expected (DevOps seeds with a script analogous to
`sym_gateway/scripts/seed_brave_search.py`):
    POST {gateway}/proxy/firecrawl/scrape
    POST {gateway}/proxy/firecrawl/crawl

The gateway envelope mirrors Brave's:
    {"method": "POST", "path": "<firecrawl-api-path>", "body": {...}}

When Firecrawl is not yet seeded in SymGateway, the call returns HTTP 404 or
400 from the gateway; we surface a clear error pointing to DevOps so the
caller knows whether the fault is theirs or the infra's.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from symbiote.browser.config import SearchOptions

logger = logging.getLogger(__name__)


class FirecrawlViaSymGateway:
    """Firecrawl scrape + crawl, proxied through SymGateway."""

    name = "firecrawl"

    def __init__(self, options: SearchOptions) -> None:
        self._options = options
        gateway = options.resolved_gateway_url()
        self._scrape_endpoint = f"{gateway}/proxy/firecrawl/scrape"
        self._crawl_endpoint = f"{gateway}/proxy/firecrawl/crawl"
        self._key = options.resolved_api_key()

    async def extract(self, url: str) -> dict[str, Any]:
        body = {
            "method": "POST",
            "path": "/v1/scrape",
            "body": {"url": url, "formats": ["markdown"]},
        }
        envelope = await self._post(self._scrape_endpoint, body)
        page = (envelope.get("body") or {}).get("data") or {}
        return {
            "url": url,
            "title": page.get("metadata", {}).get("title", ""),
            "content": page.get("markdown") or page.get("content") or "",
            "language": page.get("metadata", {}).get("language"),
            "platform": "firecrawl",
            "metadata": page.get("metadata") or {},
        }

    async def crawl(
        self,
        domain: str,
        *,
        instruction: str,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        body = {
            "method": "POST",
            "path": "/v1/crawl",
            "body": {
                "url": domain,
                "limit": max(1, min(100, int(max_pages))),
                "scrapeOptions": {"formats": ["markdown"]},
                "prompt": instruction,
            },
        }
        envelope = await self._post(self._crawl_endpoint, body)
        # Firecrawl /v1/crawl returns a job; for v1 simplicity we treat the
        # synchronous endpoint as definitive. Per their docs, /v1/crawl-status
        # is the async variant — wire that in v0.6.1 if needed.
        pages = (envelope.get("body") or {}).get("data") or []
        results: list[dict[str, Any]] = []
        for page in pages[:max_pages]:
            results.append(
                {
                    "url": page.get("metadata", {}).get("sourceURL", ""),
                    "title": page.get("metadata", {}).get("title", ""),
                    "content": page.get("markdown") or page.get("content") or "",
                }
            )
        return results

    async def _post(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._options.timeout_seconds) as client:
            response = await client.post(
                endpoint,
                headers={"Authorization": f"Bearer {self._key}"},
                json=body,
            )
            if response.status_code == 404:
                raise RuntimeError(
                    "Firecrawl provider not configured in SymGateway. "
                    "Ask DevOps to run a seed script analogous to "
                    "sym_gateway/scripts/seed_brave_search.py for Firecrawl."
                )
            response.raise_for_status()
            envelope = response.json()

        status = envelope.get("status_code", 0)
        if status >= 400:
            raise RuntimeError(
                f"Firecrawl via SymGateway failed: status={status} "
                f"body={envelope.get('body')}"
            )
        return envelope
