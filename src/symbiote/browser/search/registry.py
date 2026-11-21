"""Which providers back ``web_search``, ``web_extract`` and ``web_crawl``.

No search engine is built into the kernel's tools: the host chooses. It
either passes its own provider (any object with the method of the protocol in
``symbiote.browser.search.providers.base``) or the name of a registered one::

    from symbiote.browser import register, register_search_provider

    class MySearch:
        name = "my-search"
        async def search(self, query, *, limit=5):
            return [{"url": ..., "title": ..., "snippet": ...}]

    register(kernel, search_backend=MySearch())             # an object
    register_search_provider("my-search", lambda options: MySearch())
    register(kernel, search_backend="my-search")            # or a name

A factory receives the ``SearchOptions`` given to ``register`` and returns the
provider. The names that ship with the package are registered below; the ones
that need SymGateway say so, the others talk to their service directly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from symbiote.browser.config import SearchOptions

ProviderFactory = Callable[[SearchOptions], Any]

# kind -> the method a provider of that kind must have
_METHODS = {"search": "search", "extract": "extract", "crawl": "crawl"}
_FACTORIES: dict[str, dict[str, ProviderFactory]] = {"search": {}, "extract": {}, "crawl": {}}


def _register(kind: str, name: str, factory: ProviderFactory) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{kind} provider name must be a non-empty string")
    if not callable(factory):
        raise TypeError(f"{kind} provider factory for {name!r} must be callable")
    _FACTORIES[kind][name] = factory


def register_search_provider(name: str, factory: ProviderFactory) -> None:
    """Make ``search_backend=name`` build its provider with ``factory(options)``."""
    _register("search", name, factory)


def register_extract_provider(name: str, factory: ProviderFactory) -> None:
    """Make ``extract_backend=name`` build its provider with ``factory(options)``."""
    _register("extract", name, factory)


def register_crawl_provider(name: str, factory: ProviderFactory) -> None:
    """Make ``crawl_backend=name`` build its provider with ``factory(options)``."""
    _register("crawl", name, factory)


def provider_names(kind: str) -> list[str]:
    """Registered names for ``kind`` (``search``, ``extract`` or ``crawl``)."""
    return sorted(_FACTORIES[kind])


def resolve_provider(kind: str, backend: Any, options: SearchOptions) -> Any:
    """The provider for ``backend``: a registered name, or the host's own
    object (returned as it is, once it shows the method of its protocol)."""
    method = _METHODS[kind]
    if isinstance(backend, str):
        factory = _FACTORIES[kind].get(backend)
        if factory is None:
            raise ValueError(
                f"Unknown {kind} backend {backend!r}. Registered: {provider_names(kind)}. "
                f"Pass a provider object, or register one with "
                f"register_{kind}_provider(name, factory)."
            )
        return factory(options)
    if not callable(getattr(backend, method, None)):
        raise TypeError(
            f"A {kind} provider must have an async {method}() method "
            f"(see symbiote.browser.search.providers.base); got {type(backend).__name__}"
        )
    return backend


# ── the providers that ship with the package (imported on first use) ────


def _brave_via_gateway(options: SearchOptions) -> Any:
    from symbiote.browser.search.providers.brave import BraveViaSymGateway

    return BraveViaSymGateway(options=options)


def _objective_via_gateway(options: SearchOptions) -> Any:
    from symbiote.browser.search.providers.symgateway_objective import (
        ObjectiveSearchViaSymGateway,
    )

    return ObjectiveSearchViaSymGateway(options=options)


def _brave_api(options: SearchOptions) -> Any:
    from symbiote.browser.search.providers.brave_api import BraveSearchAPI

    return BraveSearchAPI(options=options)


def _searxng(options: SearchOptions) -> Any:
    from symbiote.browser.search.providers.searxng import SearxngSearch

    return SearxngSearch(options=options)


def _forge_scraper(options: SearchOptions) -> Any:
    from symbiote.browser.search.providers.forge_scraper import ForgeScraperProvider

    return ForgeScraperProvider(options=options)


def _http_extract(options: SearchOptions) -> Any:
    from symbiote.browser.search.providers.http_extract import HttpExtract

    return HttpExtract(options=options)


def _firecrawl_via_gateway(options: SearchOptions) -> Any:
    from symbiote.browser.search.providers.firecrawl import FirecrawlViaSymGateway

    return FirecrawlViaSymGateway(options=options)


# Through SymGateway (need SYMGATEWAY_BASE_URL / SYMGATEWAY_API_KEY).
register_search_provider("brave", _brave_via_gateway)
register_search_provider("symgateway_objective", _objective_via_gateway)
register_extract_provider("firecrawl", _firecrawl_via_gateway)
register_crawl_provider("firecrawl", _firecrawl_via_gateway)
# Direct: no gateway.
register_search_provider("brave_api", _brave_api)
register_search_provider("searxng", _searxng)
register_extract_provider("forge_scraper", _forge_scraper)
register_extract_provider("http", _http_extract)


__all__ = [
    "ProviderFactory",
    "provider_names",
    "register_crawl_provider",
    "register_extract_provider",
    "register_search_provider",
    "resolve_provider",
]
