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
* ``current_spawn()`` returns a ``SpawnContext`` while a delegated symbiote
  is working (the ``spawn`` tool): who delegated, to whom and how deep.
  ``None`` outside a delegation. Hooks, the LLM adapter, the tool guard and
  tool handlers fire for the delegated symbiote inside the turn of the one
  that delegated; this is how they tell the two apart.

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

    ``approval_id`` is set when the call is the execution of a request that
    waited for approval (``kernel.resolve_pending``): a person already said
    yes to exactly this call, outside any turn.
    """

    symbiote_id: str
    session_id: str | None = None
    host: Mapping[str, Any] = field(default=_EMPTY_HOST)
    approval_id: str | None = None


_current: contextvars.ContextVar[ToolExecContext | None] = contextvars.ContextVar(
    "symbiote_tool_exec_context", default=None
)
_host: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar(
    "symbiote_host_context", default=_EMPTY_HOST
)


@dataclass(frozen=True)
class SpawnContext:
    """A delegation in progress: one symbiote working for another.

    ``symbiote_id`` / ``session_id`` are the delegated symbiote and the
    sub-session opened for the task. ``parent_*`` is who called ``spawn``.
    ``root_*`` is the turn the host started, the top of the chain (equal to
    the parent in a direct delegation). ``depth`` is 1 for a direct
    delegation, 2 when the delegated symbiote delegates again.
    """

    symbiote_id: str
    session_id: str
    parent_symbiote_id: str | None
    parent_session_id: str | None
    root_symbiote_id: str | None
    root_session_id: str | None
    depth: int


_spawn: contextvars.ContextVar[SpawnContext | None] = contextvars.ContextVar(
    "symbiote_spawn_context", default=None
)


def current_spawn() -> SpawnContext | None:
    """Return the delegation the running code belongs to, if any."""
    return _spawn.get()


def bind_spawn(spawn: SpawnContext) -> contextvars.Token:
    """Mark the current context as working inside ``spawn``; returns a
    reset token."""
    return _spawn.set(spawn)


def reset_spawn(token: contextvars.Token) -> None:
    """Undo ``bind_spawn`` (call in a ``finally`` block)."""
    _spawn.reset(token)


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
def tool_exec_context(
    symbiote_id: str, session_id: str | None, approval_id: str | None = None
) -> Iterator[None]:
    token = _current.set(
        ToolExecContext(
            symbiote_id=symbiote_id,
            session_id=session_id,
            host=_host.get(),
            approval_id=approval_id,
        )
    )
    try:
        yield
    finally:
        _current.reset(token)


__all__ = [
    "SpawnContext",
    "ToolExecContext",
    "bind_host_context",
    "bind_spawn",
    "current_host_context",
    "current_spawn",
    "current_tool_context",
    "reset_spawn",
    "reset_host_context",
    "tool_exec_context",
]
