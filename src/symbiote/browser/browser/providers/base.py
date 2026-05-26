"""BrowserProvider protocol — contract for browser backend implementations."""

from __future__ import annotations

from typing import Protocol


class BrowserSession(Protocol):
    """A single isolated browser session bound to a task_id."""

    task_id: str

    def navigate(self, url: str) -> str: ...
    def snapshot(self) -> str: ...
    def click(self, ref: str) -> None: ...
    def fill(self, ref: str, value: str) -> None: ...
    def screenshot(self, *, full_page: bool = False) -> bytes: ...
    def close(self) -> None: ...


class BrowserProvider(Protocol):
    """Contract for browser backend implementations.

    Phase 2 implements Chromium local. Phase 3 adds Browserbase, Browser Use.
    """

    name: str

    def get_or_create_session(self, task_id: str) -> BrowserSession:
        """Return the session for *task_id*, creating it on first use."""
        ...

    def close_session(self, task_id: str) -> None:
        """Close and release the session for *task_id* if it exists."""
        ...

    def close_all(self) -> None:
        """Close every active session (used by atexit cleanup)."""
        ...
