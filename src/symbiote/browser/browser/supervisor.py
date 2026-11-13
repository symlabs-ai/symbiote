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
import os
import signal
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from symbiote.browser.browser.providers.base import BrowserProvider

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_providers: list[BrowserProvider] = []
_handlers_installed = False
# SIGTERM handler that was in place before ours (called after cleanup).
_previous_sigterm: object = None


def register_provider(provider: BrowserProvider) -> None:
    """Register a provider so its sessions get closed at interpreter exit."""
    global _handlers_installed
    with _lock:
        _providers.append(provider)
        if not _handlers_installed:
            atexit.register(_shutdown_all)
            # Not in main thread or signal not supported — fall back to atexit only.
            with contextlib.suppress(ValueError, OSError):
                _install_sigterm_handler()
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


def _install_sigterm_handler() -> None:
    global _previous_sigterm
    previous = signal.getsignal(signal.SIGTERM)
    if previous is signal.SIG_IGN:
        return  # the host chose to ignore SIGTERM; atexit still cleans up
    _previous_sigterm = previous
    signal.signal(signal.SIGTERM, _signal_handler)


def _signal_handler(signum, frame) -> None:
    """Close browser sessions, then let SIGTERM do what it did before.

    A host handler installed earlier is called; with no handler (the
    default action) the default is restored and the signal re-raised, so
    the process still terminates instead of ignoring SIGTERM.
    """
    logger.info("Browser supervisor: caught signal %s, closing sessions", signum)
    _shutdown_all()
    previous = _previous_sigterm
    if callable(previous):
        previous(signum, frame)
        return
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
