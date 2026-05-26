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
    CrawlBackend,
    ExtractBackend,
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
    extract_backend: ExtractBackend | list[ExtractBackend] | None = None,
    crawl_backend: CrawlBackend | None = None,
    browser_backend: BrowserBackend | None = None,
    browser_options: dict[str, Any] | BrowserOptions | None = None,
    policy: dict[str, Any] | PolicyConfig | None = None,
    stealth: bool = False,
) -> None:
    """Register websearch / extract / crawl / browser tools on a SymbioteKernel.

    Args:
        kernel: The Symbiote kernel to extend.
        search_backend: Search provider for `web_search` (e.g. "brave"). None disables.
        search_routing: Per-tool search provider overrides.
        search_options: Search tool behavior (limits, SymGateway overrides).
        extract_backend: Extract provider(s) for `web_extract`. Pass a list
            to chain them with first-non-empty-wins fallback
            (e.g. ["forge_scraper", "firecrawl"]). None disables.
        crawl_backend: Crawl provider for `web_crawl` (e.g. "firecrawl"). None disables.
        browser_backend: Browser provider for browser_* tools. None disables.
        browser_options: Browser tool behavior (headed, timeouts, viewport).
        policy: Domain blocklist/allowlist applied to web/browser tools.
        stealth: Enable anti-fingerprint extras (requires [stealth] extra).

    Idempotent: calling `register` twice on the same kernel is a no-op.
    """
    if id(kernel) in _REGISTERED_KERNELS:
        return

    # Eagerly normalize so misshaped input fails fast with a useful error,
    # even if no backend ends up active.
    _normalize_search_options(search_options)
    _normalize_browser_options(browser_options)
    _normalize_routing(search_routing, search_backend)
    policy_obj = _build_policy(policy)

    any_search_tool = (
        search_backend is not None
        or search_routing is not None
        or extract_backend is not None
        or crawl_backend is not None
    )
    if not any_search_tool and browser_backend is None:
        _REGISTERED_KERNELS.add(id(kernel))
        return

    if any_search_tool:
        _register_search(
            kernel,
            search_backend=search_backend,
            search_routing=search_routing,
            search_options=search_options,
            extract_backend=extract_backend,
            crawl_backend=crawl_backend,
        )

    if browser_backend is not None:
        _register_browser(
            kernel,
            browser_backend,
            browser_options,
            stealth,
            policy=policy_obj,
        )

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
    *,
    search_backend: SearchBackend | None,
    search_routing: dict[str, SearchBackend] | SearchRouting | None,
    search_options: dict[str, Any] | SearchOptions | None,
    extract_backend: ExtractBackend | list[ExtractBackend] | None,
    crawl_backend: CrawlBackend | None,
) -> None:
    """Register web_search / web_extract / web_crawl backed by the chosen providers.

    Each tool registers only when its backend is requested:
        - search_backend → web_search
        - extract_backend → web_extract (single or chained providers)
        - crawl_backend → web_crawl
    """
    opts = _normalize_search_options(search_options)
    search_provider = _build_search_provider(search_backend, search_routing, opts)
    extract_provider = _build_extract_provider(extract_backend, opts)
    crawl_provider = _build_crawl_provider(crawl_backend, opts)

    from symbiote.browser.search.tools import (
        WEB_CRAWL_DESCRIPTOR,
        WEB_EXTRACT_DESCRIPTOR,
        WEB_SEARCH_DESCRIPTOR,
        build_handlers,
    )

    handlers = build_handlers(
        search=search_provider,
        extract=extract_provider,
        crawl=crawl_provider,
    )
    descriptor_map = {
        WEB_SEARCH_DESCRIPTOR.tool_id: WEB_SEARCH_DESCRIPTOR,
        WEB_EXTRACT_DESCRIPTOR.tool_id: WEB_EXTRACT_DESCRIPTOR,
        WEB_CRAWL_DESCRIPTOR.tool_id: WEB_CRAWL_DESCRIPTOR,
    }
    for tool_id, handler in handlers.items():
        kernel._tool_gateway.register_descriptor(  # noqa: SLF001
            descriptor=descriptor_map[tool_id],
            handler=handler,
        )


def _build_search_provider(
    backend: SearchBackend | None,
    routing: dict[str, SearchBackend] | SearchRouting | None,
    opts: SearchOptions,
) -> Any:
    if backend is None and routing is None:
        return None
    resolved = backend or "brave"
    if resolved != "brave":
        raise NotImplementedError(
            f"Search backend {resolved!r} not implemented yet."
        )
    from symbiote.browser.search.providers.brave import BraveViaSymGateway

    return BraveViaSymGateway(options=opts)


def _build_extract_provider(
    backend: ExtractBackend | list[ExtractBackend] | None,
    opts: SearchOptions,
) -> Any:
    if backend is None:
        return None
    if isinstance(backend, str):
        return _single_extract_provider(backend, opts)
    if not backend:
        raise ValueError("extract_backend list cannot be empty")
    if len(backend) == 1:
        return _single_extract_provider(backend[0], opts)

    from symbiote.browser.search.extract_chain import ExtractWithFallback

    providers = [_single_extract_provider(name, opts) for name in backend]
    return ExtractWithFallback(providers)


def _single_extract_provider(name: ExtractBackend, opts: SearchOptions) -> Any:
    if name == "forge_scraper":
        from symbiote.browser.search.providers.forge_scraper import (
            ForgeScraperProvider,
        )

        return ForgeScraperProvider(options=opts)
    if name == "firecrawl":
        from symbiote.browser.search.providers.firecrawl import (
            FirecrawlViaSymGateway,
        )

        return FirecrawlViaSymGateway(options=opts)
    raise NotImplementedError(f"Extract backend {name!r} not implemented")


def _build_crawl_provider(
    backend: CrawlBackend | None, opts: SearchOptions
) -> Any:
    if backend is None:
        return None
    if backend == "firecrawl":
        from symbiote.browser.search.providers.firecrawl import (
            FirecrawlViaSymGateway,
        )

        return FirecrawlViaSymGateway(options=opts)
    raise NotImplementedError(f"Crawl backend {backend!r} not implemented")


def _register_browser(
    kernel: SymbioteKernel,
    backend: BrowserBackend,
    options: dict[str, Any] | BrowserOptions | None,
    stealth: bool,
    *,
    policy: Any = None,
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
            "Stealth mode arrives in a future phase; install with pip install \"symbiote[stealth]\""
        )

    from symbiote.browser.browser.tools import ALL_DESCRIPTORS, build_handlers

    handlers = build_handlers(provider, policy=policy)
    for descriptor in ALL_DESCRIPTORS:
        kernel._tool_gateway.register_descriptor(  # noqa: SLF001
            descriptor=descriptor,
            handler=handlers[descriptor.tool_id],
        )


def _build_policy(policy: dict[str, Any] | PolicyConfig | None) -> Any:
    """Instantiate WebsitePolicy from the config, or None when policy is unset."""
    if policy is None:
        return None
    config = policy if isinstance(policy, PolicyConfig) else PolicyConfig(**policy)
    from symbiote.browser.hooks.website_policy import WebsitePolicy

    return WebsitePolicy(config)
