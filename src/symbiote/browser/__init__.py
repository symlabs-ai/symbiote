"""symbiote.browser — websearch and browser navigation tools.

Opt-in subpackage. Importing `symbiote` does NOT import this module.
Activate by calling `register(kernel, ...)` from your host code.

Web search has no built-in engine: the host passes its own provider, or the
name of a registered one (`register_search_provider`). The package ships
`brave_api` and `searxng`, which talk to their service directly, and `brave`
/ `symgateway_objective`, which go through SymGateway and need
`SYMGATEWAY_API_KEY` / `SYMGATEWAY_BASE_URL`.

Browser navigation needs Playwright, kept in the `[browser]` extra so
clients that only want search pay no cost:

- `pip install "symbiote[browser]"` — Playwright + Chromium runtime
- `pip install "symbiote[stealth]"` — anti-fingerprint browser extras

See docs/integrations/symbiote-browser-quickstart.md for usage.
"""

from symbiote.browser.register import (
    default_chrome_profile,
    find_chrome,
    register,
    register_browser,
    register_user_browser,
)
from symbiote.browser.search.fetch import FetchedPage, register_fetcher
from symbiote.browser.search.registry import (
    register_crawl_provider,
    register_extract_provider,
    register_search_provider,
)

__all__ = [
    "FetchedPage",
    "default_chrome_profile",
    "find_chrome",
    "register",
    "register_browser",
    "register_crawl_provider",
    "register_extract_provider",
    "register_fetcher",
    "register_search_provider",
    "register_user_browser",
]
