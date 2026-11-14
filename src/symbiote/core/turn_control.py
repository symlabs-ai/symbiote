"""Stopping a turn from the outside, before its next step.

A turn is a loop of steps: a model call, then tool calls, then another model
call. The kernel binds one ``TurnControl`` to each turn (a context variable,
so it reaches worker threads and tasks of that turn only). Before every step
the loop calls ``check_turn()``; when the control says the turn has to stop,
it raises ``TurnStop`` and no further step starts. A step already running is
not interrupted.

Two things stop a turn: the session lock was lost (``LOCK_LOST``) or the
host cancelled it with ``kernel.cancel_turn`` (``CANCELLED``). The runner
that was executing attaches the trace of what it had done so far to the
``TurnStop`` on its way up.

``TurnStop`` derives from ``BaseException``, like ``asyncio.CancelledError``:
the loop's own error handling (retries, turning a failed call into an error
result) catches ``Exception`` and must not swallow it. It never leaves the
kernel: ``kernel.message()`` turns it into a regular exception.
"""

from __future__ import annotations

import contextvars
import threading
from typing import Any


class TurnStop(BaseException):  # noqa: N818 - a signal, not an error
    """Raised inside a turn to stop it before its next step."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
        # ``LoopTrace`` of what the turn did before stopping, set by the
        # runner (the innermost one first; an outer runner may replace it).
        self.trace: Any = None


LOCK_LOST = "session lock lost"
CANCELLED = "cancelled"


class TurnControl:
    """Why the turn in progress must stop, if it must."""

    def __init__(self) -> None:
        self._lock_lost = threading.Event()
        self._cancelled = threading.Event()

    def lose_lock(self) -> None:
        """The session lock could not be renewed: another replica may run
        this session now."""
        self._lock_lost.set()

    def cancel(self) -> None:
        """The host asked for this turn to stop."""
        self._cancelled.set()

    @property
    def lock_lost(self) -> bool:
        return self._lock_lost.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def check(self) -> None:
        if self._lock_lost.is_set():
            raise TurnStop(LOCK_LOST)
        if self._cancelled.is_set():
            raise TurnStop(CANCELLED)


_current: contextvars.ContextVar[TurnControl | None] = contextvars.ContextVar(
    "symbiote_turn_control", default=None
)


def bind_turn_control(control: TurnControl) -> contextvars.Token:
    return _current.set(control)


def reset_turn_control(token: contextvars.Token) -> None:
    _current.reset(token)


def current_turn_control() -> TurnControl | None:
    return _current.get()


def check_turn() -> None:
    """Raise ``TurnStop`` if the turn in progress has to stop. Called by the
    loop before each model call and each tool call; a no-op outside a turn."""
    control = _current.get()
    if control is not None:
        control.check()


__all__ = [
    "CANCELLED",
    "LOCK_LOST",
    "TurnControl",
    "TurnStop",
    "bind_turn_control",
    "check_turn",
    "current_turn_control",
    "reset_turn_control",
]
