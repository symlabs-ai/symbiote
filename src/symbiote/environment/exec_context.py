"""Execution context for tool handlers.

The ``ToolGateway`` sets this before running any handler, so builtin
handlers know *which* symbiote/session is calling without trusting
parameters supplied by the LLM. The policy gate copies the current
``contextvars`` context into worker threads, so the value is visible in
sync, async and parallel execution paths alike.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolExecContext:
    symbiote_id: str
    session_id: str | None = None


_current: contextvars.ContextVar[ToolExecContext | None] = contextvars.ContextVar(
    "symbiote_tool_exec_context", default=None
)


def current_tool_context() -> ToolExecContext | None:
    """Return the context of the tool call being executed, if any."""
    return _current.get()


@contextmanager
def tool_exec_context(symbiote_id: str, session_id: str | None) -> Iterator[None]:
    token = _current.set(ToolExecContext(symbiote_id=symbiote_id, session_id=session_id))
    try:
        yield
    finally:
        _current.reset(token)
