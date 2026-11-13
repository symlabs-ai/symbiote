"""Caller context for tool handlers, approval callbacks and the tool guard.

Public contract (hosts may rely on it):

* ``kernel.message(..., host_context={...})`` / ``message_async`` bind an
  opaque, host-owned mapping to that one turn. It is never rendered into the
  prompt, never persisted and never logged by the kernel.
* ``current_host_context()`` returns that mapping anywhere inside the turn:
  the LLM adapter, lifecycle hooks, the approval callback, the tool guard and
  every tool handler.
* ``current_tool_context()`` returns a ``ToolExecContext`` (symbiote, session
  and the host mapping) while a tool handler, the approval callback or the
  tool guard is running; ``None`` elsewhere.

Propagation is guaranteed on every execution path: the calling thread, the
daemon threads that run sync handlers (``run_in_daemon_thread`` copies the
caller's ``contextvars``), the gateway event loop that awaits async handlers
from sync code, ``asyncio.to_thread`` workers and ``asyncio`` tasks. Two turns
running at the same time never observe each other's context: each thread and
each task has its own copy.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

_EMPTY_HOST: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class ToolExecContext:
    """Who is calling: the symbiote, the session and the host's own data.

    ``host`` is the read-only mapping given as ``host_context`` to
    ``kernel.message()`` / ``message_async()`` for the turn in progress
    (empty when the host gave none).
    """

    symbiote_id: str
    session_id: str | None = None
    host: Mapping[str, Any] = field(default=_EMPTY_HOST)


_current: contextvars.ContextVar[ToolExecContext | None] = contextvars.ContextVar(
    "symbiote_tool_exec_context", default=None
)
_host: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar(
    "symbiote_host_context", default=_EMPTY_HOST
)


def current_tool_context() -> ToolExecContext | None:
    """Return the context of the tool call being executed, if any."""
    return _current.get()


def current_host_context() -> Mapping[str, Any]:
    """Return the host mapping bound to the turn in progress (empty if none)."""
    return _host.get()


def bind_host_context(host_context: Mapping[str, Any] | None) -> contextvars.Token | None:
    """Bind ``host_context`` to the current turn; returns a reset token.

    ``None`` binds nothing and returns ``None``: the turn keeps whatever is
    already bound, so a nested turn (a sub-agent started from a tool handler)
    inherits the context of the turn that started it.
    """
    if host_context is None:
        return None
    return _host.set(MappingProxyType(dict(host_context)))


def reset_host_context(token: contextvars.Token | None) -> None:
    """Undo ``bind_host_context`` (call in a ``finally`` block)."""
    if token is not None:
        _host.reset(token)


@contextmanager
def tool_exec_context(symbiote_id: str, session_id: str | None) -> Iterator[None]:
    token = _current.set(
        ToolExecContext(
            symbiote_id=symbiote_id, session_id=session_id, host=_host.get()
        )
    )
    try:
        yield
    finally:
        _current.reset(token)


__all__ = [
    "ToolExecContext",
    "bind_host_context",
    "current_host_context",
    "current_tool_context",
    "reset_host_context",
    "tool_exec_context",
]
