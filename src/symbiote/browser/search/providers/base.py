"""SearchProvider protocol — contract for search backend implementations.

All providers run async so handlers register directly on ToolGateway's async
execution path (no thread switch).
"""

from __future__ import annotations

from typing import Any, Protocol


class SearchProvider(Protocol):
    """Contract for search backend implementations.

    Phase 1 implements `search()` only (Brave via SymGateway).
    Phase 4 will add `extract()` and `crawl()`.
    """

    name: str

    async def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Run a web search and return normalized results.

        Returns:
            List of dicts with keys: url, title, snippet.
        """
        ...
