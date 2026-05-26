"""symbiote.browser — websearch and browser navigation tools.

Opt-in subpackage. Importing `symbiote` does NOT import this module.
Activate by calling `register(kernel, ...)` from your host code.

Web search goes through SymGateway's centralized provider proxy (Brave by
default) — no extra SDK needed, no per-tool credentials. Just the existing
`SYMGATEWAY_API_KEY` / `SYMGATEWAY_BASE_URL` from the host's `.env`.

Browser navigation needs Playwright, kept in the `[browser]` extra so
clients that only want search pay no cost:

- `pip install "symbiote[browser]"` — Playwright + Chromium runtime
- `pip install "symbiote[stealth]"` — anti-fingerprint browser extras

See docs/integrations/symbiote-browser-quickstart.md for usage.
"""

from symbiote.browser.register import register

__all__ = ["register"]
