"""SearchProvider protocol — contract for search backend implementations."""

from __future__ import annotations

from typing import Any, Protocol


class SearchResult(Protocol):
    """Shape of a single search result item."""

    url: str
    title: str
    snippet: str
    score: float | None


class SearchProvider(Protocol):
    """Contract for search backend implementations.

    Phase 1 implements `search()`. Phase 4 adds `extract()` and `crawl()`.
    """

    name: str

    def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Run a web search and return normalized results.

        Args:
            query: Natural-language search query.
            limit: Max number of results to return.

        Returns:
            List of dicts with keys: url, title, snippet, score (optional).
        """
        ...

    def extract(
        self, urls: list[str], *, fmt: str = "markdown"
    ) -> list[dict[str, Any]]:
        """Extract clean content from specific URLs.

        Args:
            urls: Pages to extract.
            fmt: Output format ("markdown" | "text").

        Returns:
            List of dicts with keys: url, content, title (optional).
        """
        ...

    def crawl(
        self, domain: str, *, instruction: str, max_pages: int = 10
    ) -> list[dict[str, Any]]:
        """Crawl a domain following a natural-language instruction.

        Args:
            domain: Domain or starting URL to crawl.
            instruction: What to look for / what to extract.
            max_pages: Cap on pages crawled.

        Returns:
            List of dicts with keys: url, content, title (optional).
        """
        ...
