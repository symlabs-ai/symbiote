"""Local Chromium provider via Playwright async API.

Async by design: Playwright sync has thread-affinity, while the kernel's
PolicyGate executes each tool call in a fresh worker thread. Using the async
API lets every handler run directly in the event loop without thread switches.

Each task_id maps to one isolated BrowserContext + Page. The context is
created lazily on first use and cleaned up by `close_session(task_id)` or
`close_all()` (the supervisor calls the latter at interpreter exit).

The heavy import (`playwright.async_api`) lives in `_lazy_playwright()` so that
this module can be imported without Playwright installed — only `register()`
with `browser_backend="chromium"` actually exercises the runtime.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from symbiote.browser.browser.snapshot import SnapshotResult, render_snapshot
from symbiote.browser.browser.supervisor import register_provider, unregister_provider
from symbiote.browser.config import BrowserOptions

if TYPE_CHECKING:
    from playwright.async_api import (
        Browser,
        BrowserContext,
        Page,
        Playwright,
    )

logger = logging.getLogger(__name__)


def _lazy_playwright() -> Any:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover — guarded at register
        raise ImportError(
            "Playwright is not installed. Run: pip install \"symbiote[browser]\""
            " && playwright install chromium"
        ) from exc
    return async_playwright


class ChromiumSession:
    """One isolated Chromium context+page bound to a task_id (async API)."""

    def __init__(
        self,
        task_id: str,
        context: BrowserContext,
        page: Page,
        options: BrowserOptions,
    ) -> None:
        self.task_id = task_id
        self._context = context
        self._page = page
        self._last_snapshot: SnapshotResult | None = None
        self._timeout_ms = options.timeout_ms

    async def navigate(self, url: str) -> str:
        await self._page.goto(url, timeout=self._timeout_ms)
        return self._page.url

    async def snapshot(self) -> SnapshotResult:
        aria_text = await self._page.locator("body").aria_snapshot()
        self._last_snapshot = render_snapshot(aria_text)
        return self._last_snapshot

    async def click(self, ref: str) -> None:
        self._require_ref(ref)
        node = self._last_snapshot.refs[ref]
        locator = self._locator_for(node)
        await locator.click(timeout=self._timeout_ms)

    async def fill(self, ref: str, value: str) -> None:
        self._require_ref(ref)
        node = self._last_snapshot.refs[ref]
        locator = self._locator_for(node)
        await locator.fill(value, timeout=self._timeout_ms)

    async def screenshot(self, *, full_page: bool = False) -> bytes:
        return await self._page.screenshot(full_page=full_page, type="png")

    async def wait_for(self, text: str, *, timeout_ms: int | None = None) -> None:
        loc = self._page.get_by_text(text).first
        await loc.wait_for(timeout=timeout_ms or self._timeout_ms)

    async def close(self) -> None:
        try:
            await self._context.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Closing context for %s failed: %s", self.task_id, exc)

    def _require_ref(self, ref: str) -> None:
        if self._last_snapshot is None:
            raise RuntimeError(
                f"No snapshot taken yet for task {self.task_id} — "
                "call browser_snapshot before clicking/filling"
            )
        if ref not in self._last_snapshot.refs:
            raise KeyError(
                f"Unknown ref {ref!r}. Valid refs from last snapshot: "
                f"{sorted(self._last_snapshot.refs)[:10]}"
            )

    def _locator_for(self, node: dict[str, Any]) -> Any:
        role = node["role"]
        name = node.get("name") or ""
        if name:
            return self._page.get_by_role(role, name=name).first
        return self._page.get_by_role(role).first


class ChromiumProvider:
    """Async provider for Chromium via Playwright."""

    name = "chromium"

    def __init__(self, options: BrowserOptions | None = None) -> None:
        self._options = options or BrowserOptions()
        self._lock = asyncio.Lock()
        self._sessions: dict[str, ChromiumSession] = {}
        self._playwright: Playwright | None = None
        self._playwright_cm: Any = None
        self._browser: Browser | None = None
        register_provider(self)

    async def get_or_create_session(self, task_id: str) -> ChromiumSession:
        async with self._lock:
            session = self._sessions.get(task_id)
            if session is not None:
                return session
            await self._ensure_browser()
            assert self._browser is not None
            context = await self._browser.new_context(
                viewport={
                    "width": self._options.viewport_width,
                    "height": self._options.viewport_height,
                },
            )
            page = await context.new_page()
            session = ChromiumSession(task_id, context, page, self._options)
            self._sessions[task_id] = session
            return session

    async def close_session(self, task_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(task_id, None)
        if session is not None:
            await session.close()

    async def close_all_async(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            try:
                await s.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Session %s close failed: %s", s.task_id, exc)
        try:
            if self._browser is not None:
                await self._browser.close()
                self._browser = None
            if self._playwright_cm is not None:
                await self._playwright_cm.__aexit__(None, None, None)
                self._playwright_cm = None
                self._playwright = None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Playwright shutdown failed: %s", exc)
        unregister_provider(self)

    def close_all(self) -> None:
        """Synchronous cleanup entrypoint for atexit / signal handler."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Best effort — schedule shutdown; can't block here.
                asyncio.ensure_future(self.close_all_async())
                return
        except RuntimeError:
            pass
        try:
            asyncio.run(self.close_all_async())
        except Exception as exc:  # noqa: BLE001
            logger.warning("close_all_async failed: %s", exc)

    async def _ensure_browser(self) -> None:
        if self._browser is not None:
            return
        async_playwright = _lazy_playwright()
        self._playwright_cm = async_playwright()
        self._playwright = await self._playwright_cm.__aenter__()
        self._browser = await self._playwright.chromium.launch(
            headless=not self._options.headed,
            slow_mo=self._options.slow_mo,
        )
