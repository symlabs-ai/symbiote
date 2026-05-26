"""Brave Search via SymGateway proxy.

SymGateway centralizes provider credentials and billing in the Symlabs
ecosystem. We never see or store the actual Brave API key — we just forward
through `POST {gateway}/proxy/brave/web-search` with the host's SymGateway
bearer token (the same one used for LLM calls).

Endpoint signature:
    POST {gateway}/proxy/brave/web-search
    Authorization: Bearer <SYMGATEWAY_API_KEY>
    Body: {
        "method": "GET",
        "path": "/res/v1/web/search",
        "query_params": {"q": "...", "count": "5"}
    }

Gateway response (the part we care about):
    {
        "status_code": 200,
        "body": { ...Brave Search JSON... },
        "elapsed_ms": int,
        "cost_usd": float
    }

We normalize Brave's web results into a flat list of {url, title, snippet}.
The full Brave payload (FAQ, videos, news) is intentionally dropped here —
agents that need the raw payload can use the gateway directly via an HTTP
tool, but the default `web_search` aims to be compact for the LLM context.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from symbiote.browser.config import SearchOptions

logger = logging.getLogger(__name__)


class BraveViaSymGateway:
    """SearchProvider that forwards to SymGateway's Brave proxy."""

    name = "brave"

    def __init__(self, options: SearchOptions) -> None:
        self._options = options
        self._endpoint = f"{options.resolved_gateway_url()}/proxy/brave/web-search"
        self._key = options.resolved_api_key()

    async def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        limit = max(1, min(20, int(limit)))
        body = {
            "method": "GET",
            "path": "/res/v1/web/search",
            "query_params": {"q": query, "count": str(limit)},
        }
        async with httpx.AsyncClient(timeout=self._options.timeout_seconds) as client:
            response = await client.post(
                self._endpoint,
                headers={"Authorization": f"Bearer {self._key}"},
                json=body,
            )
            response.raise_for_status()
            envelope = response.json()

        if envelope.get("status_code", 0) >= 400:
            raise RuntimeError(
                f"Brave search via SymGateway failed: status={envelope.get('status_code')} "
                f"body={envelope.get('body')}"
            )

        cost = envelope.get("cost_usd")
        if cost is not None:
            logger.info(
                "web_search query=%r results<=%d cost=$%.4f elapsed=%sms",
                query[:60],
                limit,
                cost,
                envelope.get("elapsed_ms"),
            )

        return self._normalize(envelope.get("body") or {})

    def _normalize(self, brave_body: dict[str, Any]) -> list[dict[str, Any]]:
        web = brave_body.get("web") or {}
        items = web.get("results") or []
        results: list[dict[str, Any]] = []
        for item in items:
            results.append(
                {
                    "url": item.get("url", ""),
                    "title": item.get("title", ""),
                    "snippet": item.get("description", ""),
                }
            )
        return results
