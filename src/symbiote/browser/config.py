"""Configuration models for symbiote.browser registration."""

from __future__ import annotations

import os
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

# A registered provider name (``symbiote.browser.search.registry``). The
# host may pass a provider object instead of a name wherever one is expected.
SearchBackend = str
ExtractBackend = str
CrawlBackend = str
BrowserBackend = Literal["chromium", "browserbase", "browser_use"]


class SearchOptions(BaseModel):
    """Options affecting search tool behavior.

    ``provider_options`` holds what each provider needs, keyed by the
    provider's name (``{"brave_api": {"api_key": "..."}}``). The
    ``symgateway_*`` fields are read only by the providers that go through
    SymGateway (``brave``, ``symgateway_objective``, ``firecrawl``), which
    fall back to SYMGATEWAY_BASE_URL and SYMGATEWAY_API_KEY in the host's
    environment.
    """

    max_results_default: int = 5
    compress_results: bool = True
    provider_options: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """Per-provider settings, keyed by provider name."""
    symgateway_base_url: str | None = None
    """Override SymGateway base URL. Defaults to env SYMGATEWAY_BASE_URL.
    The /v1 suffix used for LLM calls is stripped automatically for proxy calls."""
    symgateway_api_key: str | None = None
    """Override SymGateway API key. Defaults to env SYMGATEWAY_API_KEY."""
    timeout_seconds: float = 30.0
    symgateway_objective: str | None = None
    """Name of the SymGateway Objective that provides web search (e.g.
    "web_seek"). Required by ``search_backend="symgateway_objective"``."""
    symgateway_objective_service: str = "websearch"
    """The Objective's service to invoke; it receives ``{"q", "count"}``."""
    fetcher: Any = None
    """Who brings the page for the providers that read the web themselves
    (the ``http`` extractor and the search engine readers): the host's own
    object with ``async fetch(url, *, headers=None)``, or the name of a
    registered one. ``None``: the package's plain client (``"http"``). See
    ``symbiote.browser.search.fetch``."""
    max_page_bytes: int = 2_000_000
    """How much of a page those providers read; the rest is dropped."""
    max_content_chars: int = 40_000
    """How long the content returned by the ``http`` extractor may be. A
    longer page keeps its beginning and its end, with a line saying how much
    was left out. ``0``: no limit."""
    cache_ttl_seconds: float = 0.0
    """Keep the answers of ``web_search`` and ``web_extract`` in memory for
    this long, so the same query or address asked again in the meantime does
    not go out again. ``0`` (default): no cache."""

    def options_for(self, provider: str) -> dict[str, Any]:
        """The settings given for one provider (empty when none)."""
        return dict(self.provider_options.get(provider) or {})

    def resolved_gateway_url(self) -> str:
        url = self.symgateway_base_url or os.getenv("SYMGATEWAY_BASE_URL", "")
        if not url:
            raise ValueError(
                "SymGateway base URL not set. Provide search_options.symgateway_base_url "
                "or set SYMGATEWAY_BASE_URL in env."
            )
        # /proxy/<provider>/... lives at the root, not under /v1 (callers
        # that need a /v1 path add it themselves)
        return url.rstrip("/").removesuffix("/v1")

    def resolved_api_key(self) -> str:
        key = self.symgateway_api_key or os.getenv("SYMGATEWAY_API_KEY", "")
        if not key:
            raise ValueError(
                "SymGateway API key not set. Provide search_options.symgateway_api_key "
                "or set SYMGATEWAY_API_KEY in env."
            )
        return key


class BrowserOptions(BaseModel):
    """Options affecting browser tool behavior."""

    headed: bool = False
    slow_mo: int = 0
    viewport_width: int = 1280
    viewport_height: int = 800
    timeout_ms: int = 30000
    isolated_session_per_task: bool = True
    cdp_url: str | None = None
    """Attach to a Chrome already running on this machine (started with
    ``--remote-debugging-port``) instead of launching an isolated Chromium,
    e.g. ``"http://127.0.0.1:9222"``. The browser_* tools then work in new tabs
    of that browser's own profile, with its cookies and logins: they act as
    whoever is logged in there. In this mode ``browser_click`` and
    ``browser_fill`` are high risk (they go through the approval callback),
    and closing a session closes only the tabs it opened, never the browser.
    Loopback addresses only."""
    cdp_port_file: str | None = None
    """Path of a Chrome profile's ``DevToolsActivePort`` file: attach to the
    browser that is already open on that profile, with the person's tabs and
    logins. Chrome writes the file while remote debugging is allowed (Chrome
    144+: ``chrome://inspect/#remote-debugging``) and asks the person to
    allow each new connection. Read at every connection; use instead of
    ``cdp_url``."""
    cdp_connect_timeout_s: float | None = None
    """How long a connection may take; ``None`` = ``timeout_ms``. Leave room
    for the person to answer Chrome's permission dialog."""
    cdp_launch: list[str] | None = None
    """Command that starts the browser when nothing listens at ``cdp_url``:
    the binary and its flags, with the same debugging port and a profile of
    its own. The browser starts detached and stays open after the kernel
    shuts down, as the person's browser does. Requires ``cdp_url``."""
    cdp_launch_timeout_s: float = 20.0
    """How long to wait for a launched browser to open its debugging port."""
    allowlist: list[str] | None = None
    """Domains the browser may open, as in ``PolicyConfig.allowlist``
    (``example.com`` covers its subdomains, ``*.example.com`` only them).
    Checked on ``browser_navigate`` and on every page load of a tab the tools
    opened, including links clicked; ``None`` = no limit beyond the shared
    policy. Recommended with ``cdp_url``."""

    @property
    def attached(self) -> bool:
        """The tools work in a browser that is not theirs (cdp_url / cdp_port_file)."""
        return bool(self.cdp_url or self.cdp_port_file)

    @model_validator(mode="after")
    def _launch_needs_an_address(self) -> BrowserOptions:
        if self.cdp_launch and not self.cdp_url:
            raise ValueError("cdp_launch needs cdp_url: the address the launched browser listens on")
        if self.cdp_port_file and (self.cdp_url or self.cdp_launch):
            raise ValueError("cdp_port_file attaches to the browser already open: no cdp_url or cdp_launch")
        return self

    @field_validator("cdp_url")
    @classmethod
    def _local_browser_only(cls, value: str | None) -> str | None:
        if not value:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https", "ws", "wss"):
            raise ValueError(f"cdp_url must be an http(s) or ws(s) URL, got {value!r}")
        # The debugging port gives full control of the browser: only one on
        # this machine, never over the network.
        if (parsed.hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(f"cdp_url must point to this machine (127.0.0.1), got {value!r}")
        return value


class PolicyConfig(BaseModel):
    """URL access policy applied to all web/browser tools."""

    blocklist: list[str] = Field(default_factory=list)
    allowlist: list[str] | None = None
    allow_internal: bool = False
    ttl_seconds: int = 300


class SearchRouting(BaseModel):
    """Per-tool search provider routing.

    When None for a tool, the top-level `search_backend` is used. Each entry
    is a registered provider name or the host's own provider object.
    """

    web_search: Any = None
    web_extract: Any = None
    web_crawl: Any = None
