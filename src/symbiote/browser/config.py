"""Configuration models for symbiote.browser registration."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SearchBackend = Literal["tavily", "exa", "firecrawl", "parallel"]
BrowserBackend = Literal["chromium", "browserbase", "browser_use"]


class SearchOptions(BaseModel):
    """Options affecting search tool behavior."""

    max_results_default: int = 5
    compress_results: bool = True


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
