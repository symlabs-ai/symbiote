"""Public entrypoint for symbiote.browser.

`register(kernel, ...)` is the one-line API the host calls to add websearch
and/or browser-navigation tools to a SymbioteKernel instance. Everything is
opt-in; without this call, the kernel behaves exactly as before.

Heavy imports (Playwright, search SDKs) are LAZY — they happen inside the
provider-specific factories, never at module import. This guarantees that
`import symbiote` and even `import symbiote.browser` stay cheap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from symbiote.browser.config import (
    BrowserBackend,
    BrowserOptions,
    PolicyConfig,
    SearchBackend,
    SearchOptions,
    SearchRouting,
)

if TYPE_CHECKING:
    from symbiote.core.kernel import SymbioteKernel


_REGISTERED_KERNELS: set[int] = set()


def register(
    kernel: SymbioteKernel,
    *,
    search_backend: SearchBackend | None = None,
    search_routing: dict[str, SearchBackend] | SearchRouting | None = None,
    search_options: dict[str, Any] | SearchOptions | None = None,
    browser_backend: BrowserBackend | None = None,
    browser_options: dict[str, Any] | BrowserOptions | None = None,
    policy: dict[str, Any] | PolicyConfig | None = None,
    stealth: bool = False,
) -> None:
    """Register websearch and/or browser tools on a SymbioteKernel.

    Args:
        kernel: The Symbiote kernel to extend.
        search_backend: Default search provider; set to None to disable search.
        search_routing: Per-tool provider overrides (web_search/extract/crawl).
        search_options: Search tool behavior (limits, compression).
        browser_backend: Browser provider; set to None to disable browser.
        browser_options: Browser tool behavior (headed, timeouts, viewport).
        policy: Domain blocklist/allowlist applied to all web/browser tools.
        stealth: Enable anti-fingerprint extras (requires [stealth] extra).

    Idempotent: calling `register` twice on the same kernel is a no-op.
    """
    if id(kernel) in _REGISTERED_KERNELS:
        return

    _normalize_search_options(search_options)
    _normalize_browser_options(browser_options)
    _normalize_policy(policy)
    _normalize_routing(search_routing, search_backend)

    if search_backend is None and search_routing is None and browser_backend is None:
        _REGISTERED_KERNELS.add(id(kernel))
        return

    if search_backend is not None or search_routing is not None:
        _register_search(kernel, search_backend, search_routing, search_options)

    if browser_backend is not None:
        _register_browser(kernel, browser_backend, browser_options, stealth)

    if policy is not None:
        _register_policy_hook(kernel, policy)

    _REGISTERED_KERNELS.add(id(kernel))


def _normalize_search_options(
    options: dict[str, Any] | SearchOptions | None,
) -> SearchOptions:
    if options is None:
        return SearchOptions()
    if isinstance(options, SearchOptions):
        return options
    return SearchOptions(**options)


def _normalize_browser_options(
    options: dict[str, Any] | BrowserOptions | None,
) -> BrowserOptions:
    if options is None:
        return BrowserOptions()
    if isinstance(options, BrowserOptions):
        return options
    return BrowserOptions(**options)


def _normalize_policy(
    policy: dict[str, Any] | PolicyConfig | None,
) -> PolicyConfig | None:
    if policy is None:
        return None
    if isinstance(policy, PolicyConfig):
        return policy
    return PolicyConfig(**policy)


def _normalize_routing(
    routing: dict[str, SearchBackend] | SearchRouting | None,
    default_backend: SearchBackend | None,
) -> SearchRouting:
    if routing is None:
        return SearchRouting(
            web_search=default_backend,
            web_extract=default_backend,
            web_crawl=default_backend,
        )
    if isinstance(routing, SearchRouting):
        return routing
    return SearchRouting(**routing)


def _register_search(
    kernel: SymbioteKernel,
    backend: SearchBackend | None,
    routing: dict[str, SearchBackend] | SearchRouting | None,
    options: dict[str, Any] | SearchOptions | None,
) -> None:
    """Phase 1+: register web_search, web_extract, web_crawl tools. NO-OP for now."""
    return


def _register_browser(
    kernel: SymbioteKernel,
    backend: BrowserBackend,
    options: dict[str, Any] | BrowserOptions | None,
    stealth: bool,
) -> None:
    """Register browser_* tools backed by the chosen provider."""
    opts = _normalize_browser_options(options)
    if backend == "chromium":
        from symbiote.browser.browser.providers.chromium_local import ChromiumProvider

        provider = ChromiumProvider(options=opts)
    elif backend in ("browserbase", "browser_use"):
        raise NotImplementedError(
            f"Browser backend {backend!r} is planned for Phase 3 (cloud providers)"
        )
    else:
        raise ValueError(f"Unknown browser_backend: {backend!r}")

    if stealth:
        raise NotImplementedError(
            "Stealth mode arrives in Phase 5; install with pip install \"symbiote[stealth]\""
        )

    from symbiote.browser.browser.tools import ALL_DESCRIPTORS, build_handlers

    handlers = build_handlers(provider)
    for descriptor in ALL_DESCRIPTORS:
        kernel._tool_gateway.register_descriptor(  # noqa: SLF001
            descriptor=descriptor,
            handler=handlers[descriptor.tool_id],
        )


def _register_policy_hook(
    kernel: SymbioteKernel,
    policy: dict[str, Any] | PolicyConfig,
) -> None:
    """Phase 1+: install WebsitePolicyHook on kernel.hooks. NO-OP for now."""
    return
