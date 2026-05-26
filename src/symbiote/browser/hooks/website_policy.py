"""Website access policy — blocklist/allowlist with wildcard matching and TTL cache.

Applied at the handler boundary for every URL-taking tool (browser_navigate
primarily; web_extract / web_crawl when those land in Phase 4). The check
runs *before* the SSRF validator so policy decisions are visible in the
audit log even when DNS resolution would succeed.

Matching rules (case-insensitive):
- An entry matches a URL's hostname.
- Plain entry like ``example.com`` matches the exact host AND any subdomain
  (``foo.example.com``).
- Wildcard ``*.example.com`` matches any subdomain but NOT the bare host.
- ``allowlist=None`` means "everything not in blocklist is allowed".
- ``allowlist=[...]`` means "only those (and their subdomains) are allowed".
- ``blocklist`` always wins over ``allowlist`` for the same domain.

Errors raised:
- ``DomainBlockedError`` when a URL is denied by policy.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

from symbiote.browser.config import PolicyConfig

logger = logging.getLogger(__name__)


class DomainBlockedError(PermissionError):
    """Raised when a URL is denied by WebsitePolicy."""


class WebsitePolicy:
    """Domain access policy.

    Thread-safe at the read side: matchers are parsed once at construction
    or after TTL expiry. Concurrent callers may see briefly stale state, but
    never an inconsistent matcher.
    """

    def __init__(self, config: PolicyConfig) -> None:
        self._config = config
        self._parsed_at: float = 0.0
        self._block_exact: set[str] = set()
        self._block_suffix: set[str] = set()  # for *.example.com style
        self._allow_exact: set[str] | None = None
        self._allow_suffix: set[str] | None = None
        self._reparse()

    def _reparse(self) -> None:
        block_exact, block_suffix = self._split_patterns(self._config.blocklist)
        self._block_exact = block_exact
        self._block_suffix = block_suffix
        if self._config.allowlist is None:
            self._allow_exact = None
            self._allow_suffix = None
        else:
            ae, as_ = self._split_patterns(self._config.allowlist)
            self._allow_exact = ae
            self._allow_suffix = as_
        self._parsed_at = time.monotonic()

    def _maybe_reparse(self) -> None:
        if (time.monotonic() - self._parsed_at) > self._config.ttl_seconds:
            self._reparse()

    @staticmethod
    def _split_patterns(patterns: list[str]) -> tuple[set[str], set[str]]:
        """Split a pattern list into (exact-match, suffix-match) sets, lower-cased.

        Plain ``example.com`` goes into BOTH exact and suffix sets (so it
        matches itself and subdomains). Wildcard ``*.example.com`` goes only
        into suffix.
        """
        exact: set[str] = set()
        suffix: set[str] = set()
        for raw in patterns:
            p = raw.strip().lower()
            if not p:
                continue
            if p.startswith("*."):
                suffix.add(p[2:])
            else:
                exact.add(p)
                suffix.add(p)
        return exact, suffix

    @staticmethod
    def _matches(host: str, exact: set[str], suffix: set[str]) -> bool:
        if host in exact:
            return True
        # Walk PARENT domains only (skip the bare host) so wildcard patterns
        # like ``*.ads.com`` don't accidentally match the bare ``ads.com``.
        # Plain patterns are also added to ``exact`` above, so a plain
        # ``ads.com`` still matches itself.
        parts = host.split(".")
        for i in range(1, len(parts)):
            candidate = ".".join(parts[i:])
            if candidate in suffix:
                return True
        return False

    def check(self, url: str) -> None:
        """Raise DomainBlockedError if *url* is denied by policy.

        Returns normally when the URL is allowed.
        """
        self._maybe_reparse()
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if not host:
            raise DomainBlockedError(f"No hostname in URL: {url!r}")

        # Allow loopback / private nets if explicitly enabled — useful for
        # dev/test against local HTTP servers. Real SSRF check still runs
        # separately in the handler.
        if self._config.allow_internal and host in {"localhost", "127.0.0.1", "::1"}:
            return

        if self._matches(host, self._block_exact, self._block_suffix):
            logger.info("WebsitePolicy: blocked %r (blocklist)", host)
            raise DomainBlockedError(f"Domain blocked by policy: {host}")

        if self._allow_exact is not None or self._allow_suffix is not None:
            allowed = self._matches(
                host,
                self._allow_exact or set(),
                self._allow_suffix or set(),
            )
            if not allowed:
                logger.info("WebsitePolicy: blocked %r (not in allowlist)", host)
                raise DomainBlockedError(
                    f"Domain not in allowlist: {host}"
                )
