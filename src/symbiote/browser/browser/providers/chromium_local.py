"""Local Chromium provider via Playwright async API.

Async by design: Playwright sync has thread-affinity, while the kernel's
PolicyGate executes each tool call in a fresh worker thread. Using the async
API lets every handler run directly in the event loop without thread switches.

Each task_id maps to one isolated BrowserContext + Page. The context is
created lazily on first use and cleaned up by `close_session(task_id)` or
`close_all()` (the supervisor calls the latter at interpreter exit).

With ``BrowserOptions.cdp_url`` (or ``cdp_port_file``, for the Chrome the
person has open) the provider attaches to a Chrome that is already running
instead: each task_id gets a new tab in that browser's own
profile (its cookies and logins), and closing releases only those tabs.

The heavy import (`playwright.async_api`) lives in `_lazy_playwright()` so that
this module can be imported without Playwright installed — only `register()`
with `browser_backend="chromium"` actually exercises the runtime.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from symbiote.browser.browser.snapshot import SnapshotResult, render_snapshot
from symbiote.browser.browser.supervisor import register_provider, unregister_provider
from symbiote.browser.config import BrowserOptions

if TYPE_CHECKING:
    from playwright.async_api import (
        Browser,
        BrowserContext,
        Page,
        Playwright,
        Request,
        Route,
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
        *,
        owns_context: bool = True,
    ) -> None:
        self.task_id = task_id
        self._context = context
        self._page = page
        self._last_snapshot: SnapshotResult | None = None
        self._timeout_ms = options.timeout_ms
        # False when the context is the attached browser's own profile: only
        # this session's tab is ours to close.
        self._owns_context = owns_context
        self._blocked: str | None = None

    @property
    def closed(self) -> bool:
        """The tab is gone (closed by the person, or the browser went away)."""
        return self._page.is_closed()

    async def guard_navigation(self, check: Callable[[str], None]) -> None:
        """Abort every page load of this tab whose URL ``check`` refuses:
        the address the tools open and the links clicked on the page."""

        async def route(route: Route, request: Request) -> None:
            if request.is_navigation_request() and request.frame.parent_frame is None:
                url = request.url
                if urlparse(url).scheme in ("http", "https"):
                    try:
                        check(url)
                    except Exception as exc:  # noqa: BLE001 — any refusal blocks
                        self._blocked = f"{urlparse(url).hostname}: {exc}"
                        logger.info("Browser %s: blocked %s (%s)", self.task_id, url[:120], exc)
                        await route.abort("blockedbyclient")
                        return
            await route.continue_()

        await self._page.route("**/*", route)

    async def navigate(self, url: str) -> str:
        await self._page.goto(url, timeout=self._timeout_ms)
        return self._page.url

    async def snapshot(self) -> SnapshotResult:
        aria_text = await self._page.locator("body").aria_snapshot()
        self._last_snapshot = render_snapshot(aria_text)
        return self._last_snapshot

    async def click(self, ref: str) -> None:
        node = self._require_ref(ref)
        locator = self._locator_for(node)
        self._blocked = None
        await locator.click(timeout=self._timeout_ms)
        if self._blocked:
            # The click led to a page outside the allowlist. Chrome shows an
            # error page in its place: go back to where the click was, and
            # say so, or the model reads that page as the result of its click.
            blocked = self._blocked
            try:
                await self._page.go_back(timeout=self._timeout_ms)
            except Exception as exc:  # noqa: BLE001 — the error below says what matters
                logger.debug("Browser %s: going back failed: %s", self.task_id, exc)
            raise PermissionError(f"Navigation blocked by the browser allowlist ({blocked})")

    async def fill(self, ref: str, value: str) -> None:
        node = self._require_ref(ref)
        locator = self._locator_for(node)
        await locator.fill(value, timeout=self._timeout_ms)

    async def screenshot(self, *, full_page: bool = False) -> bytes:
        return await self._page.screenshot(full_page=full_page, type="png")

    async def wait_for(self, text: str, *, timeout_ms: int | None = None) -> None:
        loc = self._page.get_by_text(text).first
        await loc.wait_for(timeout=timeout_ms or self._timeout_ms)

    async def close(self) -> None:
        try:
            if self._owns_context:
                await self._context.close()
            elif not self._page.is_closed():
                await self._page.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Closing context for %s failed: %s", self.task_id, exc)

    def _require_ref(self, ref: str) -> dict[str, Any]:
        """The node for *ref* in the last snapshot (raises if there is none)."""
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
        return self._last_snapshot.refs[ref]

    def _locator_for(self, node: dict[str, Any]) -> Any:
        role = node["role"]
        name = node.get("name") or ""
        if name:
            return self._page.get_by_role(role, name=name).first
        return self._page.get_by_role(role).first


class ChromiumProvider:
    """Async provider for Chromium via Playwright."""

    name = "chromium"

    def __init__(
        self,
        options: BrowserOptions | None = None,
        *,
        nav_guard: Callable[[str], None] | None = None,
    ) -> None:
        self._options = options or BrowserOptions()
        # Raises for a URL a tab must not load (see ChromiumSession.guard_navigation).
        self._nav_guard = nav_guard
        self._lock = asyncio.Lock()
        self._sessions: dict[str, ChromiumSession] = {}
        self._playwright: Playwright | None = None
        self._playwright_cm: Any = None
        self._browser: Browser | None = None
        # Playwright objects belong to the event loop that created them: the
        # browser is closed there, whichever thread asks (see close_all).
        self._loop: asyncio.AbstractEventLoop | None = None
        register_provider(self)

    async def get_or_create_session(self, task_id: str) -> ChromiumSession:
        async with self._lock:
            session = self._sessions.get(task_id)
            if session is not None and not session.closed:
                return session
            await self._ensure_browser()
            assert self._browser is not None
            if self._options.attached:
                # The attached browser's first context is its own profile:
                # the tab opens there, with the cookies and logins it holds.
                contexts = self._browser.contexts
                context = contexts[0] if contexts else await self._browser.new_context()
                owns_context = False
            else:
                context = await self._browser.new_context(
                    viewport={
                        "width": self._options.viewport_width,
                        "height": self._options.viewport_height,
                    },
                )
                owns_context = True
            page = await context.new_page()
            session = ChromiumSession(
                task_id, context, page, self._options, owns_context=owns_context
            )
            if self._nav_guard is not None:
                await session.guard_navigation(self._nav_guard)
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
                # Launched: closes Chromium. Attached (cdp_url): only
                # disconnects; the person's browser and tabs stay open.
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
        """Synchronous cleanup: kernel shutdown, atexit and signal handler.

        Runs ``close_all_async`` on the loop the browser was started on. From
        another thread (a kernel shut down from a worker thread while that
        loop serves requests) it waits for it; from that loop's own thread it
        can only schedule it.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            # Nothing started, or its objects went with their loop.
            unregister_provider(self)
            return
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        try:
            if loop.is_running():
                if current is loop:
                    loop.create_task(self.close_all_async())  # best effort: can't block here
                    return
                asyncio.run_coroutine_threadsafe(self.close_all_async(), loop).result(timeout=15)
            else:
                loop.run_until_complete(self.close_all_async())
        except Exception as exc:  # noqa: BLE001
            logger.warning("close_all_async failed: %s", exc)

    async def _attach_open_session(self, port_file: Path) -> Browser:
        """Attach to the browser open on a profile, through the address it
        publishes in ``DevToolsActivePort`` while remote debugging is allowed."""
        try:
            lines = port_file.read_text(encoding="utf-8").split()
            endpoint = f"ws://127.0.0.1:{int(lines[0])}{lines[1]}"
        except (OSError, ValueError, IndexError) as exc:
            raise RuntimeError(
                "The user's Chrome is not accepting a connection: remote debugging is "
                f"off ({port_file} not found or unreadable). Ask the user to open "
                "chrome://inspect/#remote-debugging in their Chrome (version 144 or "
                "newer), turn on remote debugging, and then try again; Chrome will ask "
                "them to allow the connection."
            ) from exc
        timeout_s = self._options.cdp_connect_timeout_s
        timeout_ms = timeout_s * 1000 if timeout_s else self._options.timeout_ms
        try:
            # Chrome holds the connection until the person answers its dialog.
            return await self._connect(endpoint, timeout_ms)
        except Exception as exc:
            raise RuntimeError(
                "Could not connect to the user's Chrome. Chrome asks them to allow the "
                "connection: ask the user to click Allow in the dialog Chrome shows, "
                f"then try again ({type(exc).__name__}: {exc})"
            ) from exc

    async def _connect(self, cdp_url: str, timeout_ms: float) -> Browser:
        assert self._playwright is not None
        return await self._playwright.chromium.connect_over_cdp(
            cdp_url, timeout=timeout_ms, slow_mo=self._options.slow_mo
        )

    async def _attach(self, cdp_url: str) -> Browser:
        """Connect to the browser at ``cdp_url``; with ``cdp_launch``, start
        it first when nothing is listening there."""
        try:
            return await self._connect(cdp_url, self._options.timeout_ms)
        except Exception as exc:
            if not self._options.cdp_launch:
                raise RuntimeError(
                    f"No browser to attach to at {cdp_url}: start Chrome with "
                    f"--remote-debugging-port and a profile of its own ({exc})"
                ) from exc
            first_error = exc
        command = self._options.cdp_launch
        logger.info("Browser: nothing at %s, starting %s", cdp_url, command[0])
        try:
            # Detached: it is the person's browser now, and outlives the kernel.
            subprocess.Popen(  # noqa: S603 — the host's configured command
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
            )
        except OSError as exc:
            raise RuntimeError(f"Could not start the browser ({command[0]}): {exc}") from exc
        deadline = time.monotonic() + self._options.cdp_launch_timeout_s
        while True:
            await asyncio.sleep(0.3)
            try:
                return await self._connect(cdp_url, 2000)
            except Exception as exc:  # noqa: BLE001 — still starting
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        f"Started {command[0]}, but nothing answered at {cdp_url} after "
                        f"{self._options.cdp_launch_timeout_s:.0f}s ({exc}; first: {first_error})"
                    ) from exc

    async def _ensure_browser(self) -> None:
        if self._browser is not None:
            if self._browser.is_connected():
                return
            # The attached browser was closed: its tabs are gone, and the
            # next call attaches again (to a browser started again).
            self._sessions.clear()
            self._browser = None
        if self._playwright is None:
            self._loop = asyncio.get_running_loop()
            async_playwright = _lazy_playwright()
            self._playwright_cm = async_playwright()
            self._playwright = await self._playwright_cm.__aenter__()
        assert self._playwright is not None
        if self._options.cdp_port_file:
            self._browser = await self._attach_open_session(Path(self._options.cdp_port_file))
            return
        cdp_url = self._options.cdp_url
        if cdp_url:
            self._browser = await self._attach(cdp_url)
            return
        self._browser = await self._playwright.chromium.launch(
            headless=not self._options.headed,
            slow_mo=self._options.slow_mo,
        )
