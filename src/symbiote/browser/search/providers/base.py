"""Provider protocols for search, extract, and crawl.

All providers run async so handlers register directly on ToolGateway's async
execution path (no thread switch). The three concerns live in separate
protocols so a backend can implement any subset (Brave: search only;
forge_scraper: extract only; Firecrawl: extract + crawl).
"""

from __future__ import annotations

from typing import Any, Protocol


class SearchProvider(Protocol):
    """Backend that runs web search queries (Brave via SymGateway, etc)."""

    name: str

    async def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Run a web search and return normalized results.

        Returns:
            List of dicts with keys: url, title, snippet.
        """
        ...


class ExtractProvider(Protocol):
    """Backend that extracts clean content from a specific URL."""

    name: str

    async def extract(self, url: str) -> dict[str, Any]:
        """Extract content from a single URL.

        Returns:
            Dict with keys: url, title, content (markdown/text),
            language? (ISO code), platform? (youtube/reddit/twitter/...),
            metadata? (dict). Raises on hard failure; returns dict with
            empty `content` when the page yielded nothing useful so the
            caller can decide to fall back.
        """
        ...


class CrawlProvider(Protocol):
    """Backend that crawls a domain following a natural-language instruction."""

    name: str

    async def crawl(
        self,
        domain: str,
        *,
        instruction: str,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        """Crawl pages matching the instruction.

        Returns:
            List of dicts with keys: url, title, content.
        """
        ...
