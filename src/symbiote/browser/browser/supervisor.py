"""Browser session supervisor — manages lifecycle and cleanup across sessions.

The supervisor keeps a global registry of active BrowserProvider instances so
that interpreter exit (or SIGTERM) can close all open sessions cleanly,
preventing orphan Chromium processes.

Registration happens automatically when a provider is instantiated; no
explicit setup is needed by callers.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import signal
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from symbiote.browser.browser.providers.base import BrowserProvider

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_providers: list[BrowserProvider] = []
_handlers_installed = False


def register_provider(provider: BrowserProvider) -> None:
    """Register a provider so its sessions get closed at interpreter exit."""
    global _handlers_installed
    with _lock:
        _providers.append(provider)
        if not _handlers_installed:
            atexit.register(_shutdown_all)
            # Not in main thread or signal not supported — fall back to atexit only.
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signal.SIGTERM, _signal_handler)
            _handlers_installed = True


def unregister_provider(provider: BrowserProvider) -> None:
    """Remove a provider from the supervisor registry (after manual close_all)."""
    with _lock:
        if provider in _providers:
            _providers.remove(provider)


def _shutdown_all() -> None:
    with _lock:
        providers = list(_providers)
        _providers.clear()
    for prov in providers:
        try:
            prov.close_all()
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            logger.warning("Provider %s close_all failed: %s", prov.name, exc)


def _signal_handler(signum, frame) -> None:  # noqa: ARG001
    logger.info("Browser supervisor: caught signal %s, closing sessions", signum)
    _shutdown_all()
