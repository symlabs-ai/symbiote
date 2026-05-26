"""forge_scraper-backed ExtractProvider.

`forge_scraper.get_content(url)` is a platform-aware facade in the Symlabs
ecosystem (`~/dev/libs/forge_scraper`). It dispatches to specialized
extractors for YouTube (transcript), Reddit (post + comments), Twitter,
Instagram, and falls back to a generic HTML extractor for everything else.

The library is synchronous (requests + BeautifulSoup based), so each call
runs through `asyncio.to_thread` to keep the event loop free. SymGateway
relay is already wired inside the lib (since v0.10.5) for residential
proxies — no extra plumbing on our side.

Requires the `[extract]` extra: `pip install "symbiote[extract]"`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from symbiote.browser.config import SearchOptions

logger = logging.getLogger(__name__)


def _lazy_forge_scraper() -> Any:
    try:
        import forge_scraper  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover — guarded at register
        raise ImportError(
            "forge_scraper is not installed. Run: "
            'pip install "symbiote[extract]"'
        ) from exc
    return forge_scraper


class ForgeScraperProvider:
    """ExtractProvider backed by `forge_scraper.get_content()`."""

    name = "forge_scraper"

    def __init__(self, options: SearchOptions | None = None) -> None:
        self._options = options or SearchOptions()

    async def extract(self, url: str) -> dict[str, Any]:
        forge = _lazy_forge_scraper()

        def _sync_extract() -> Any:
            return forge.get_content(url=url, verbose=False)

        try:
            result = await asyncio.to_thread(_sync_extract)
        except Exception as exc:  # noqa: BLE001 — surface to caller for fallback
            logger.info("forge_scraper failed for %s: %s", url, exc)
            raise

        return _normalize(result, url)


def _normalize(content_info: Any, url: str) -> dict[str, Any]:
    """Convert forge_scraper.ContentInfo into the standard {url,title,content,...} shape."""
    title = getattr(content_info, "title", "") or ""
    content = getattr(content_info, "content", "") or ""
    language = getattr(content_info, "language", None)
    metadata = getattr(content_info, "metadata", None) or {}
    platform = getattr(content_info, "platform", None)
    if platform is not None and hasattr(platform, "value"):
        platform = platform.value
    return {
        "url": url,
        "title": title,
        "content": content,
        "language": language,
        "platform": platform,
        "metadata": dict(metadata) if metadata else {},
    }
