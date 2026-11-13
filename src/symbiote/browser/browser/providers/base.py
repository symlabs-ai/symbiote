"""BrowserProvider protocol — contract for browser backend implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from symbiote.browser.browser.snapshot import SnapshotResult


class BrowserSession(Protocol):
    """A single isolated browser session bound to a task_id (async API)."""

    task_id: str

    async def navigate(self, url: str) -> str: ...
    async def snapshot(self) -> SnapshotResult: ...
    async def click(self, ref: str) -> None: ...
    async def fill(self, ref: str, value: str) -> None: ...
    async def screenshot(self, *, full_page: bool = False) -> bytes: ...
    async def wait_for(self, text: str, *, timeout_ms: int | None = None) -> None: ...
    async def close(self) -> None: ...


class BrowserProvider(Protocol):
    """Contract for browser backend implementations.

    Phase 2 implements Chromium local. Phase 3 adds Browserbase, Browser Use.
    """

    name: str

    async def get_or_create_session(self, task_id: str) -> BrowserSession:
        """Return the session for *task_id*, creating it on first use."""
        ...

    async def close_session(self, task_id: str) -> None:
        """Close and release the session for *task_id* if it exists."""
        ...

    def close_all(self) -> None:
        """Close every active session (used by atexit cleanup)."""
        ...
