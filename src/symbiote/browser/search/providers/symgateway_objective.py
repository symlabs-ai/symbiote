"""Web search through a SymGateway Objective service.

An Objective of type ``external_api`` publishes named services (e.g.
``websearch``) with an input schema; the gateway picks the provider account,
the credential and the fallbacks, so the host only names the purpose. We call

    POST {gateway}/v1/objectives/invoke
    Authorization: Bearer <SYMGATEWAY_API_KEY>
    Body: {"objective": "web_seek", "service": "websearch",
           "input": {"q": "...", "count": 5}}

and get back

    {"request_id": "...", "objective": "web_seek", "service": "websearch",
     "output": { ...what the service returns... }, "status_code": 200,
     "fallback_used": false}

The key needs ``objectives.use`` and ``external_services.invoke`` on the
gateway; ``gateway.llm.invoke`` does not imply either.

``output`` is whatever the Objective's output mapping produces. A service
bound to Brave returns Brave's JSON (``web.results[]``); one that already
normalizes returns a list of results. Both are accepted.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from symbiote.browser.config import SearchOptions

logger = logging.getLogger(__name__)


class ObjectiveSearchViaSymGateway:
    """SearchProvider that invokes a search service of a SymGateway Objective."""

    name = "symgateway_objective"

    def __init__(self, options: SearchOptions) -> None:
        objective = (options.symgateway_objective or "").strip()
        if not objective:
            raise ValueError(
                "search_backend='symgateway_objective' needs the Objective's name: "
                "set search_options.symgateway_objective (e.g. 'web_seek')."
            )
        self._options = options
        self._objective = objective
        self._service = options.symgateway_objective_service
        self._endpoint = f"{options.resolved_gateway_url()}/v1/objectives/invoke"
        self._key = options.resolved_api_key()

    async def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        limit = max(1, min(20, int(limit)))
        body = {
            "objective": self._objective,
            "service": self._service,
            "input": {"q": query, "count": limit},
        }
        async with httpx.AsyncClient(timeout=self._options.timeout_seconds) as client:
            response = await client.post(
                self._endpoint,
                headers={"Authorization": f"Bearer {self._key}"},
                json=body,
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"SymGateway refused objective {self._objective!r} / service "
                f"{self._service!r} (HTTP {response.status_code}): {_gateway_message(response)}"
            )
        envelope = response.json()
        logger.info(
            "web_search objective=%s service=%s query=%r results<=%d fallback=%s",
            self._objective,
            self._service,
            query[:60],
            limit,
            envelope.get("fallback_used"),
        )
        return _normalize(envelope.get("output"))


def _gateway_message(response: httpx.Response) -> str:
    """The gateway's own reason (``error.message``), not just the status."""
    try:
        error = response.json().get("error") or {}
    except ValueError:
        return response.text[:300]
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or error)
    return str(error)


def _normalize(output: Any) -> list[dict[str, Any]]:
    """Flatten a service output into ``[{url, title, snippet}]``."""
    if isinstance(output, dict):
        web = output.get("web")
        items = web.get("results") if isinstance(web, dict) else output.get("results")
    else:
        items = output
    results: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "url": item.get("url", ""),
                "title": item.get("title", ""),
                "snippet": item.get("snippet") or item.get("description") or "",
            }
        )
    return results
