"""SearchWithFallback — try search providers in the host's order.

``register(kernel, search_backend=["first", "second"])`` builds one of these.
A provider that fails (any exception: refused, unrelated results, network) is
skipped and the next one is asked. The first non-empty list wins.

When no provider returns results, the two remaining ends stay apart: if at
least one answered with an empty list the search ran and found nothing
(``[]``); if every one failed, ``SearchRefusedError`` says what each one did.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from symbiote.browser.search.outcomes import SearchRefusedError
from symbiote.browser.search.providers.base import SearchProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchRun:
    """One search through the chain: the results, the provider they came
    from (``None``: none) and what each provider tried before did."""

    results: list[dict[str, Any]]
    source: str | None = None
    attempts: list[str] = field(default_factory=list)


def _reason(exc: BaseException) -> str:
    text = str(exc).strip()
    return text if isinstance(exc, SearchRefusedError) and text else f"{type(exc).__name__}: {text}".rstrip(": ")


class SearchWithFallback:
    """Chain SearchProviders. First non-empty result wins."""

    def __init__(self, providers: list[SearchProvider]) -> None:
        if not providers:
            raise ValueError("SearchWithFallback requires at least one provider")
        self._providers = providers
        self.name = "+".join(p.name for p in providers)

    async def run(self, query: str, *, limit: int = 5) -> SearchRun:
        attempts: list[str] = []
        answered = False
        for provider in self._providers:
            try:
                results = await provider.search(query, limit=limit)
            except Exception as exc:  # noqa: BLE001 — fallback semantics
                attempts.append(f"{provider.name}: {_reason(exc)}")
                logger.info("search chain: %s failed: %s", provider.name, exc)
                continue
            if results:
                return SearchRun(results=results, source=provider.name, attempts=attempts)
            answered = True
            attempts.append(f"{provider.name}: no results")
        if answered:
            return SearchRun(results=[], attempts=attempts)
        raise SearchRefusedError("; ".join(attempts))

    async def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        return (await self.run(query, limit=limit)).results
