"""ExtractWithFallback — try a chain of ExtractProviders until one succeeds.

A successful extraction is one that returns a dict with non-empty `content`.
Empty content is treated like a failure so the next provider gets a chance.
Each attempt's failure is logged but never surfaced to the caller unless
*every* provider fails — in that case the last exception is re-raised.
"""

from __future__ import annotations

import logging
from typing import Any

from symbiote.browser.search.providers.base import ExtractProvider

logger = logging.getLogger(__name__)


class ExtractWithFallback:
    """Chain ExtractProviders. First non-empty result wins.

    `name` is the concatenated chain of provider names for traceability in
    audit logs (e.g. "forge_scraper+firecrawl").
    """

    def __init__(self, providers: list[ExtractProvider]) -> None:
        if not providers:
            raise ValueError("ExtractWithFallback requires at least one provider")
        self._providers = providers
        self.name = "+".join(p.name for p in providers)

    async def extract(self, url: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for idx, provider in enumerate(self._providers):
            try:
                result = await provider.extract(url)
            except Exception as exc:  # noqa: BLE001 — fallback semantics
                logger.info(
                    "extract chain: %s failed for %s (%s/%s): %s",
                    provider.name,
                    url,
                    idx + 1,
                    len(self._providers),
                    exc,
                )
                last_error = exc
                continue

            if (result.get("content") or "").strip():
                # Annotate with which provider won, useful for downstream
                # audit + debug.
                result.setdefault("extracted_by", provider.name)
                return result

            logger.info(
                "extract chain: %s returned empty content for %s, trying next",
                provider.name,
                url,
            )

        # Exhausted the chain
        if last_error is not None:
            raise last_error
        return {
            "url": url,
            "title": "",
            "content": "",
            "language": None,
            "platform": None,
            "metadata": {},
            "content_quality": None,
            "quality_reason": None,
            "extracted_by": None,
        }
