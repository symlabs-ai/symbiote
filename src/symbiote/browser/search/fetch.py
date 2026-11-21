"""Who brings a page: the one network door of the providers that read the web
themselves (the ``http`` extractor and the search engine readers).

Those providers never open a connection. They ask a *fetcher* for a URL and
get the status, the headers, the body and the address the answer came from::

    class MyFetcher:
        async def fetch(self, url, *, headers=None):
            ...
            return FetchedPage(status=200, headers={...}, body=b"...", url=final_url)

    register(kernel, extract_backend="http", search_options={"fetcher": MyFetcher()})

A host passes its own to decide where the request leaves from: a proxy, a
browser, an application on another machine. Without one the package's own
client is used (``HttpFetcher``).

**The fetcher follows the redirects and checks every hop.** Only it knows
what a name resolves to on the network it leaves from, so refusing an
internal address is its job; the fetchers of this package do it with
``symbiote.security.network``. Whoever the fetcher is, the providers cap the
time and the size of what comes back, and ``web_extract`` checks the address
the answer came from against the host's ``policy``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

MAX_REDIRECTS = 5
DEFAULT_MAX_BYTES = 2_000_000
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class FetchError(Exception):
    """The page could not be brought: network error, timeout, too many
    redirects, a hop that was refused."""


@dataclass(frozen=True)
class FetchedPage:
    """What a fetcher returns.

    ``url`` is the address the answer came from, after the redirects.
    ``headers`` are the response headers (any case); ``truncated`` says the
    body was cut at the size limit.
    """

    status: int
    body: bytes
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    truncated: bool = False

    def header(self, name: str) -> str:
        """One response header, whatever its case; ``""`` when absent."""
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return str(value)
        return ""


class PageFetcher(Protocol):
    """Brings one page with a GET, following the redirects."""

    async def fetch(
        self, url: str, *, headers: Mapping[str, str] | None = None
    ) -> FetchedPage: ...


def check_hop(url: str) -> None:
    """Refuse an address a fetcher of this package must not open: not http(s),
    or one that resolves to an internal network."""
    from symbiote.security.network import validate_url

    if urlparse(url).scheme not in ("http", "https"):
        raise FetchError(f"not an http(s) address: {url}")
    validate_url(url)


class HttpFetcher:
    """The package's own client: plain ``httpx``, no key, no gateway.

    Redirects are followed by hand so that every hop passes ``check`` (by
    default ``check_hop``: http(s) only, no internal address) before it is
    opened. The body is read up to ``max_bytes`` and the rest is dropped.
    """

    name = "http"

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_bytes: int = DEFAULT_MAX_BYTES,
        user_agent: str | None = None,
        check: Callable[[str], None] | None = None,
        transport: Any = None,
    ) -> None:
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._user_agent = user_agent or "symbiote-web/1.0"
        self._check = check or check_hop
        self._transport = transport  # tests

    async def fetch(
        self, url: str, *, headers: Mapping[str, str] | None = None
    ) -> FetchedPage:
        import httpx

        sent = {"User-Agent": self._user_agent, **(headers or {})}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, follow_redirects=False, transport=self._transport
            ) as client:
                return await asyncio.wait_for(
                    self._follow(client, url, sent), timeout=self._timeout
                )
        except TimeoutError as exc:
            raise FetchError(f"timed out after {self._timeout:g}s: {url}") from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc

    async def _follow(self, client: Any, url: str, headers: dict[str, str]) -> FetchedPage:
        for _hop in range(MAX_REDIRECTS + 1):
            self._check(url)
            async with client.stream("GET", url, headers=headers) as response:
                location = response.headers.get("location")
                if response.status_code in _REDIRECT_STATUSES and location:
                    url = urljoin(url, location)
                    continue
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_bytes:
                        break
                return FetchedPage(
                    status=response.status_code,
                    body=bytes(body[: self._max_bytes]),
                    url=url,
                    headers=dict(response.headers),
                    truncated=len(body) > self._max_bytes,
                )
        raise FetchError(f"more than {MAX_REDIRECTS} redirects")


FetcherFactory = Callable[[Any], Any]
_FETCHERS: dict[str, FetcherFactory] = {}


def register_fetcher(name: str, factory: FetcherFactory) -> None:
    """Make ``search_options.fetcher=name`` build its fetcher with
    ``factory(options)`` (the ``SearchOptions`` given to ``register``)."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("fetcher name must be a non-empty string")
    if not callable(factory):
        raise TypeError(f"fetcher factory for {name!r} must be callable")
    _FETCHERS[name] = factory


def fetcher_names() -> list[str]:
    return sorted(_FETCHERS)


def resolve_fetcher(options: Any) -> Any:
    """The fetcher of ``options``: the host's own object, a registered name,
    or (nothing chosen) the package's plain client."""
    chosen = getattr(options, "fetcher", None)
    if chosen is None:
        chosen = "http"
    if isinstance(chosen, str):
        factory = _FETCHERS.get(chosen)
        if factory is None:
            raise ValueError(
                f"Unknown fetcher {chosen!r}. Registered: {fetcher_names()}. "
                "Pass an object with an async fetch(url, *, headers=None) method."
            )
        return factory(options)
    if not callable(getattr(chosen, "fetch", None)):
        raise TypeError(
            "A fetcher must have an async fetch(url, *, headers=None) method "
            f"(see symbiote.browser.search.fetch); got {type(chosen).__name__}"
        )
    return chosen


async def fetch_page(
    fetcher: Any,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float,
    max_bytes: int,
) -> FetchedPage:
    """Ask ``fetcher`` for ``url`` on behalf of a provider.

    The same limits hold whoever the fetcher is: the call may take
    ``timeout`` seconds and the body is cut at ``max_bytes``.
    """
    try:
        page = await asyncio.wait_for(fetcher.fetch(url, headers=headers), timeout=timeout)
    except TimeoutError as exc:
        raise FetchError(f"timed out after {timeout:g}s: {url}") from exc
    if not isinstance(page, FetchedPage):
        page = FetchedPage(
            status=int(page.status),
            body=bytes(page.body),
            url=str(getattr(page, "url", "") or url),
            headers=dict(getattr(page, "headers", None) or {}),
        )
    if len(page.body) > max_bytes:
        page = FetchedPage(
            status=page.status,
            body=page.body[:max_bytes],
            url=page.url,
            headers=page.headers,
            truncated=True,
        )
    return page


def _http_fetcher(options: Any) -> HttpFetcher:
    return HttpFetcher(
        timeout=float(getattr(options, "timeout_seconds", 30.0)),
        max_bytes=int(getattr(options, "max_page_bytes", DEFAULT_MAX_BYTES)),
    )


register_fetcher("http", _http_fetcher)


__all__ = [
    "DEFAULT_MAX_BYTES",
    "FetchError",
    "FetchedPage",
    "HttpFetcher",
    "PageFetcher",
    "check_hop",
    "fetch_page",
    "fetcher_names",
    "register_fetcher",
    "resolve_fetcher",
]
