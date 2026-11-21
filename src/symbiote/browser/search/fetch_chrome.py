"""``chrome`` — a fetcher that looks like Chrome on the wire.

Some sites answer only to a browser, and tell one apart by how the
connection is opened (the TLS and HTTP/2 handshakes), not by the headers:
with browser headers alone DuckDuckGo answers 202 to every request. This
fetcher uses ``curl_cffi`` to open the connection the way Chrome does::

    register(kernel, search_backend=["duckduckgo", "yahoo"],
             search_options={"fetcher": "chrome"})

Needs the ``[web]`` extra (``pip install "symbiote[web]"``). Never chosen by
default: the host asks for it by name. Redirects are followed by hand, every
hop checked like the plain client does, and the body is read up to the size
limit.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urljoin

from symbiote.browser.search.fetch import (
    _REDIRECT_STATUSES,
    DEFAULT_MAX_BYTES,
    MAX_REDIRECTS,
    FetchedPage,
    FetchError,
    check_hop,
)


def _lazy_session() -> Any:
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError as exc:
        raise ImportError(
            'The "chrome" fetcher needs the curl_cffi package: pip install "symbiote[web]"'
        ) from exc
    return AsyncSession


class ChromeLikeFetcher:
    """PageFetcher over ``curl_cffi``, imitating the current Chrome."""

    name = "chrome"

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_bytes: int = DEFAULT_MAX_BYTES,
        profile: str = "chrome",
        check: Callable[[str], None] | None = None,
    ) -> None:
        self._session_type = _lazy_session()  # fail at register, not at the first call
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._profile = profile
        self._check = check or check_hop

    async def fetch(
        self, url: str, *, headers: Mapping[str, str] | None = None
    ) -> FetchedPage:
        try:
            async with self._session_type(impersonate=self._profile, timeout=self._timeout) as session:
                return await asyncio.wait_for(
                    self._follow(session, url, dict(headers or {})), timeout=self._timeout
                )
        except TimeoutError as exc:
            raise FetchError(f"timed out after {self._timeout:g}s: {url}") from exc
        except FetchError:
            raise
        except OSError as exc:  # curl_cffi's errors are IOError
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc

    async def _follow(self, session: Any, url: str, headers: dict[str, str]) -> FetchedPage:
        for _hop in range(MAX_REDIRECTS + 1):
            self._check(url)
            response = await session.get(url, headers=headers, allow_redirects=False, stream=True)
            try:
                location = response.headers.get("location")
                if response.status_code in _REDIRECT_STATUSES and location:
                    url = urljoin(url, location)
                    continue
                body = bytearray()
                async for chunk in response.aiter_content():
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
            finally:
                await response.aclose()
        raise FetchError(f"more than {MAX_REDIRECTS} redirects")
