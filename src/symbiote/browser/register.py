"""Public entrypoint for symbiote.browser.

`register(kernel, ...)` is the one-line API the host calls to add websearch
and/or browser-navigation tools to a SymbioteKernel instance. Everything is
opt-in; without this call, the kernel behaves exactly as before.

Heavy imports (Playwright, search SDKs) are LAZY — they happen inside the
provider-specific factories, never at module import. This guarantees that
`import symbiote` and even `import symbiote.browser` stay cheap.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

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


# Marker set on a kernel once register() ran. An attribute on the kernel
# itself, not a set of id()s: a new kernel can reuse a dead kernel's id.
_REGISTERED_ATTR = "_symbiote_browser_registered"

# Tool prefixes already registered on a kernel (see register_browser).
_PREFIXES_ATTR = "_symbiote_browser_prefixes"

# Python module that each optional backend needs, and the extra providing it.
_BACKEND_MODULES: dict[str, tuple[str, str]] = {
    "chromium": ("playwright", "browser"),
    "forge_scraper": ("forge_scraper", "extract"),
}


def register(
    kernel: SymbioteKernel,
    *,
    search_backend: SearchBackend | Any | list[SearchBackend | Any] | None = None,
    search_routing: dict[str, SearchBackend] | SearchRouting | None = None,
    search_options: dict[str, Any] | SearchOptions | None = None,
    extract_backend: ExtractBackend | Any | list[ExtractBackend | Any] | None = None,
    crawl_backend: CrawlBackend | Any | None = None,
    browser_backend: BrowserBackend | None = None,
    browser_options: dict[str, Any] | BrowserOptions | None = None,
    policy: dict[str, Any] | PolicyConfig | None = None,
    stealth: bool = False,
) -> None:
    """Register websearch / extract / crawl / browser tools on a SymbioteKernel.

    No search engine is built in. Each ``*_backend`` is either the host's own
    provider (an object with the async method of its protocol in
    ``symbiote.browser.search.providers.base``) or the name of a registered
    one (``register_search_provider`` and friends in ``symbiote.browser``).

    Args:
        kernel: The Symbiote kernel to extend.
        search_backend: Provider for `web_search`. Names that ship with the
            package: "brave_api" (the Brave Search API with the host's own
            key) and "searxng" (a SearXNG instance), both direct; "brave" and
            "symgateway_objective", both through SymGateway. Pass a list to
            try them in that order: a provider that fails is skipped and the
            first one with results answers. None disables.
        search_routing: Per-tool search provider overrides.
        search_options: Search tool behavior (limits, per-provider settings
            in ``provider_options``, SymGateway overrides).
        extract_backend: Extract provider(s) for `web_extract`. Pass a list
            to chain them with first-non-empty-wins fallback
            (e.g. ["http", "firecrawl"]). "http" reads the page itself
            (through ``search_options.fetcher``, when the host passes one)
            and "forge_scraper" runs locally; "firecrawl" goes through
            SymGateway. None disables.
        crawl_backend: Crawl provider for `web_crawl` ("firecrawl", through
            SymGateway, or the host's own). None disables.
        browser_backend: Browser provider for browser_* tools. None disables.
        browser_options: Browser tool behavior (headed, timeouts, viewport).
        policy: Domain blocklist/allowlist applied to web/browser tools.
        stealth: Enable anti-fingerprint extras (requires [stealth] extra).

    Idempotent: calling `register` twice on the same kernel is a no-op.
    """
    if getattr(kernel, _REGISTERED_ATTR, False):
        return

    # Eagerly normalize so misshaped input fails fast with a useful error,
    # even if no backend ends up active.
    _normalize_search_options(search_options)
    _normalize_browser_options(browser_options)
    _normalize_routing(search_routing, search_backend)
    policy_config = _normalize_policy(policy)
    policy_obj = _build_policy(policy_config)
    # Fail here, before any tool is registered, when a backend's extra is
    # missing — not later at the first tool call.
    extract_names = (
        list(extract_backend) if isinstance(extract_backend, list) else [extract_backend]
    )
    _require_backends([*extract_names, browser_backend])

    any_search_tool = (
        search_backend is not None
        or search_routing is not None
        or extract_backend is not None
        or crawl_backend is not None
    )
    if not any_search_tool and browser_backend is None:
        setattr(kernel, _REGISTERED_ATTR, True)
        return

    if any_search_tool:
        _register_search(
            kernel,
            search_backend=search_backend,
            search_routing=search_routing,
            search_options=search_options,
            extract_backend=extract_backend,
            crawl_backend=crawl_backend,
            policy=policy_obj,
            browser=browser_backend is not None,
        )

    if browser_backend is not None:
        _register_browser(
            kernel,
            browser_backend,
            browser_options,
            stealth,
            policy=policy_obj,
            policy_config=policy_config,
        )

    setattr(kernel, _REGISTERED_ATTR, True)


def _module_available(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


def _require_backends(backends: list[str | None]) -> None:
    for backend in backends:
        # A provider object brings its own dependencies.
        if not isinstance(backend, str) or backend not in _BACKEND_MODULES:
            continue
        module, extra = _BACKEND_MODULES[backend]
        if not _module_available(module):
            raise ImportError(
                f"Backend {backend!r} needs the {module!r} package: "
                f'pip install "symbiote[{extra}]"'
            )


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
    default_backend: Any,
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
    search_backend: SearchBackend | Any | list[SearchBackend | Any] | None,
    search_routing: dict[str, SearchBackend] | SearchRouting | None,
    search_options: dict[str, Any] | SearchOptions | None,
    extract_backend: ExtractBackend | Any | list[ExtractBackend | Any] | None,
    crawl_backend: CrawlBackend | Any | None,
    policy: Any = None,
    browser: bool = False,
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
        web_search_descriptor,
    )

    handlers = build_handlers(
        search=search_provider,
        extract=extract_provider,
        crawl=crawl_provider,
        policy=policy,
        cache_ttl=opts.cache_ttl_seconds,
    )
    # web_search points only at the reading tools registered along with it.
    readers = [WEB_EXTRACT_DESCRIPTOR.tool_id] if extract_provider is not None else []
    if browser:
        readers += ["browser_navigate", "browser_snapshot"]
    descriptor_map = {
        WEB_SEARCH_DESCRIPTOR.tool_id: web_search_descriptor(readers),
        WEB_EXTRACT_DESCRIPTOR.tool_id: WEB_EXTRACT_DESCRIPTOR,
        WEB_CRAWL_DESCRIPTOR.tool_id: WEB_CRAWL_DESCRIPTOR,
    }
    for tool_id, handler in handlers.items():
        kernel._tool_gateway.register_descriptor(  # noqa: SLF001
            descriptor=descriptor_map[tool_id],
            handler=handler,
        )


def _build_search_provider(
    backend: SearchBackend | Any | list[SearchBackend | Any] | None,
    routing: dict[str, SearchBackend] | SearchRouting | None,
    opts: SearchOptions,
) -> Any:
    """The provider of ``web_search``: ``backend`` (a list is tried in its
    order), or the one the routing names for it. There is no default engine."""
    from symbiote.browser.search.registry import resolve_provider

    if isinstance(backend, list):
        if not backend:
            raise ValueError("search_backend list cannot be empty")
        from symbiote.browser.search.search_chain import SearchWithFallback

        return SearchWithFallback([resolve_provider("search", item, opts) for item in backend])

    chosen = backend
    if chosen is None and routing is not None:
        chosen = _normalize_routing(routing, None).web_search
        if chosen is None:
            raise ValueError(
                "search_routing was given without a provider for web_search: "
                "pass search_backend, or name one in search_routing['web_search']."
            )
    if chosen is None:
        return None
    return resolve_provider("search", chosen, opts)


def _build_extract_provider(
    backend: ExtractBackend | Any | list[ExtractBackend | Any] | None,
    opts: SearchOptions,
) -> Any:
    from symbiote.browser.search.registry import resolve_provider

    if backend is None:
        return None
    if not isinstance(backend, list):
        return resolve_provider("extract", backend, opts)
    if not backend:
        raise ValueError("extract_backend list cannot be empty")
    if len(backend) == 1:
        return resolve_provider("extract", backend[0], opts)

    from symbiote.browser.search.extract_chain import ExtractWithFallback

    return ExtractWithFallback([resolve_provider("extract", item, opts) for item in backend])


def _build_crawl_provider(
    backend: CrawlBackend | Any | None, opts: SearchOptions
) -> Any:
    from symbiote.browser.search.registry import resolve_provider

    if backend is None:
        return None
    return resolve_provider("crawl", backend, opts)


def register_browser(
    kernel: SymbioteKernel,
    *,
    prefix: str = "browser",
    backend: BrowserBackend = "chromium",
    options: dict[str, Any] | BrowserOptions | None = None,
    policy: dict[str, Any] | PolicyConfig | None = None,
) -> list[str]:
    """Register one family of browser tools: ``{prefix}_navigate``,
    ``{prefix}_snapshot``, ``_click``, ``_fill``, ``_close``, ``_screenshot``
    and ``_wait_for``, each family with its own browser.

    ``register(browser_backend=...)`` registers the ``browser`` family; call
    this for another one, e.g. an isolated browser for research and the
    person's own one (``cdp_url``) for their accounts. Returns the tool ids,
    for ``environment.configure(tools=...)``. A prefix already registered on
    this kernel is a no-op that returns the same ids.
    """
    if not prefix.isidentifier():
        raise ValueError(f"prefix must be an identifier, got {prefix!r}")
    _require_backends([backend])
    policy_config = _normalize_policy(policy)
    return _register_browser(
        kernel, backend, options, False, policy=_build_policy(policy_config),
        policy_config=policy_config, prefix=prefix,
    )


def _register_browser(
    kernel: SymbioteKernel,
    backend: BrowserBackend,
    options: dict[str, Any] | BrowserOptions | None,
    stealth: bool,
    *,
    policy: Any = None,
    policy_config: PolicyConfig | None = None,
    prefix: str = "browser",
) -> list[str]:
    """Register the ``{prefix}_*`` browser tools backed by the chosen provider."""
    from symbiote.browser.browser.tools import ALL_DESCRIPTORS, build_handlers

    ids = [_with_prefix(d.tool_id, prefix) for d in ALL_DESCRIPTORS]
    done: set[str] = getattr(kernel, _PREFIXES_ATTR, set())
    if prefix in done:
        return ids
    opts = _normalize_browser_options(options)
    # The browser's own allowlist adds to the shared policy: a URL passes both.
    checks = [policy] if policy is not None else []
    if opts.allowlist is not None:
        from symbiote.browser.hooks.website_policy import WebsitePolicy

        checks.append(WebsitePolicy(PolicyConfig(
            allowlist=opts.allowlist,
            allow_internal=bool(policy_config and policy_config.allow_internal),
        )))
    browser_policy = _AllPolicies(checks) if checks else None
    if backend == "chromium":
        from symbiote.browser.browser.providers.chromium_local import ChromiumProvider

        provider = ChromiumProvider(
            options=opts,
            # Links clicked are page loads too: with an allowlist, every load
            # of a tab the tools opened is checked, not only browser_navigate.
            nav_guard=browser_policy.check if opts.allowlist is not None and browser_policy else None,
        )
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

    # Tabs opened in the person's browser close with the kernel, and a
    # launched Chromium does not outlive it.
    if hasattr(kernel, "on_shutdown"):
        kernel.on_shutdown(provider.close_all)
    handlers = build_handlers(provider, policy=browser_policy)
    for descriptor in ALL_DESCRIPTORS:
        handler = handlers[descriptor.tool_id]
        if opts.attached:
            descriptor = _attached_descriptor(descriptor)
        if prefix != "browser":
            descriptor = _prefixed_descriptor(descriptor, prefix)
        kernel._tool_gateway.register_descriptor(  # noqa: SLF001
            descriptor=descriptor,
            handler=handler,
        )
    setattr(kernel, _PREFIXES_ATTR, done | {prefix})
    return ids


def _with_prefix(tool_id: str, prefix: str) -> str:
    return prefix + tool_id.removeprefix("browser")


def _prefixed_descriptor(descriptor: Any, prefix: str) -> Any:
    """Another family: its own ids and a tag of its own instead of "browser",
    so a symbiote that sees the "browser" tag does not see this one."""
    label = prefix.replace("_", " ").title()
    return descriptor.model_copy(update={
        "tool_id": _with_prefix(descriptor.tool_id, prefix),
        "name": descriptor.name.replace("Browser", label, 1),
        "tags": [prefix, *(t for t in descriptor.tags if t != "browser")],
    })


# In the person's own browser a click or a typed value acts as them, on any
# site they are logged in to: those calls run only with a person's yes.
_ATTACHED_HIGH_RISK = frozenset({"browser_click", "browser_fill"})
_ATTACHED_NOTE = (
    " This browser is the user's own, logged in to their accounts: what you"
    " do there is done as them. Act only as the user asked. When a page asks"
    " to log in, ask the user to log in in that browser window and wait for"
    " them; never type passwords or verification codes."
)


def _attached_descriptor(descriptor: Any) -> Any:
    update: dict[str, Any] = {"description": descriptor.description + _ATTACHED_NOTE}
    if descriptor.tool_id in _ATTACHED_HIGH_RISK:
        update["risk_level"] = "high"
        update["requires_approval"] = True
    return descriptor.model_copy(update=update)


# Where register_user_browser looks for a browser, in this order.
_CHROME_CANDIDATES = (
    "google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


def find_chrome() -> str | None:
    """The first Chrome or Chromium on this machine, or None."""
    for candidate in _CHROME_CANDIDATES:
        found = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
        if found:
            return found
    return None


# The person's everyday Chrome profile, per system, in this order.
_DEFAULT_PROFILES = (
    "~/.config/google-chrome",
    "~/.config/chromium",
    "~/Library/Application Support/Google/Chrome",
    "~/Library/Application Support/Chromium",
    "~/AppData/Local/Google/Chrome/User Data",
)


def default_chrome_profile() -> Path | None:
    """The user-data directory of the person's everyday Chrome, or None."""
    for candidate in _DEFAULT_PROFILES:
        path = Path(candidate).expanduser()
        if path.is_dir():
            return path
    return None


def register_user_browser(
    kernel: SymbioteKernel,
    *,
    allowlist: list[str],
    session: Literal["open", "own"] = "own",
    port: int = 9222,
    profile_dir: str | Path | None = None,
    binary: str | None = None,
    start_url: str = "about:blank",
    prefix: str = "user_browser",
    policy: dict[str, Any] | PolicyConfig | None = None,
) -> list[str]:
    """Give the agent the person's own Chrome, with their logins, in one call.

    ``session="open"``: the Chrome the person already has open, logged in, on
    their everyday profile (``profile_dir``, default: found per system). They
    allow it once in that Chrome (``chrome://inspect/#remote-debugging``,
    Chrome 144+), and Chrome asks them to allow each new connection; the
    tools wait up to two minutes for that answer. Until it is allowed, the
    tools fail saying how to allow it.

    ``session="own"``: a Chrome with a profile of its own (``profile_dir``,
    default ``~/.config/symbiote/user-browser``) and a debugging port on
    127.0.0.1. The first call that needs it opens that Chrome, visible on
    this machine's screen; the person logs in there once and the profile
    keeps it.

    Either way the agent works in tabs of its own, only on ``allowlist``;
    clicking and typing run only with a person's approval (refused when no
    approval callback is installed). Closing releases its tabs, never the
    browser.

    For a kernel that runs on the person's machine (a desktop app, a local
    demo): on a server there is no one's screen or login to use. Returns the
    tool ids, to authorize with ``environment.configure(tools=...)``.
    """
    if not allowlist:
        raise ValueError(
            "register_user_browser needs an allowlist: a logged-in browser with no "
            "site list could act as the person anywhere"
        )
    if session == "open":
        everyday = Path(profile_dir).expanduser() if profile_dir else default_chrome_profile()
        if everyday is None:
            raise RuntimeError("No Chrome profile found on this machine; pass profile_dir=")
        open_options = BrowserOptions(
            cdp_port_file=str(everyday / "DevToolsActivePort"),
            cdp_connect_timeout_s=120.0,  # the person answers Chrome's dialog
            allowlist=allowlist,
        )
        return register_browser(kernel, prefix=prefix, backend="chromium", options=open_options, policy=policy)
    if session != "own":
        raise ValueError(f"session must be 'open' or 'own', got {session!r}")
    binary = binary or find_chrome()
    if not binary:
        raise RuntimeError("No Chrome or Chromium found on this machine; pass binary=")
    profile = Path(profile_dir) if profile_dir else Path.home() / ".config" / "symbiote" / "user-browser"
    profile.mkdir(parents=True, exist_ok=True)
    options = BrowserOptions(
        cdp_url=f"http://127.0.0.1:{port}",
        cdp_launch=[
            binary,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            start_url,
        ],
        allowlist=allowlist,
    )
    return register_browser(kernel, prefix=prefix, backend="chromium", options=options, policy=policy)


class _AllPolicies:
    """Several WebsitePolicy checks as one: a URL must pass every one."""

    def __init__(self, policies: list[Any]) -> None:
        self._policies = policies

    def check(self, url: str) -> None:
        for policy in self._policies:
            policy.check(url)


def _build_policy(policy: dict[str, Any] | PolicyConfig | None) -> Any:
    """Instantiate WebsitePolicy from the config, or None when policy is unset."""
    if policy is None:
        return None
    config = policy if isinstance(policy, PolicyConfig) else PolicyConfig(**policy)
    from symbiote.browser.hooks.website_policy import WebsitePolicy

    return WebsitePolicy(config)
