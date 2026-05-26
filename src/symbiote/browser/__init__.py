"""symbiote.browser — websearch and browser navigation tools.

Opt-in subpackage. Importing `symbiote` does NOT import this module.
Activate by calling `register(kernel, ...)` from your host code.

Heavy dependencies (Playwright, search SDKs) live in extras:
- `pip install "symbiote[search]"` — web search providers (Tavily, Firecrawl, Exa)
- `pip install "symbiote[browser]"` — Playwright + Chromium runtime
- `pip install "symbiote[stealth]"` — anti-fingerprint browser extras

See docs/integrations/symbiote-browser-quickstart.md for usage.
"""

from symbiote.browser.register import register

__all__ = ["register"]
