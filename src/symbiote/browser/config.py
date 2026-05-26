"""Configuration models for symbiote.browser registration."""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field

SearchBackend = Literal["brave", "duckduckgo"]
BrowserBackend = Literal["chromium", "browserbase", "browser_use"]


class SearchOptions(BaseModel):
    """Options affecting search tool behavior.

    Credentials come from SymGateway (centralized in the Symlabs gateway).
    By default we read SYMGATEWAY_BASE_URL and SYMGATEWAY_API_KEY from the
    host's environment, just like the LLM adapter does.
    """

    max_results_default: int = 5
    compress_results: bool = True
    symgateway_base_url: str | None = None
    """Override SymGateway base URL. Defaults to env SYMGATEWAY_BASE_URL.
    The /v1 suffix used for LLM calls is stripped automatically for proxy calls."""
    symgateway_api_key: str | None = None
    """Override SymGateway API key. Defaults to env SYMGATEWAY_API_KEY."""
    timeout_seconds: float = 30.0

    def resolved_gateway_url(self) -> str:
        url = self.symgateway_base_url or os.getenv("SYMGATEWAY_BASE_URL", "")
        if not url:
            raise ValueError(
                "SymGateway base URL not set. Provide search_options.symgateway_base_url "
                "or set SYMGATEWAY_BASE_URL in env."
            )
        # /proxy/<provider>/... lives at the root, not under /v1
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


class PolicyConfig(BaseModel):
    """URL access policy applied to all web/browser tools."""

    blocklist: list[str] = Field(default_factory=list)
    allowlist: list[str] | None = None
    allow_internal: bool = False
    ttl_seconds: int = 300


class SearchRouting(BaseModel):
    """Per-tool search provider routing.

    When None for a tool, the top-level `search_backend` is used.
    """

    web_search: SearchBackend | None = None
    web_extract: SearchBackend | None = None
    web_crawl: SearchBackend | None = None
