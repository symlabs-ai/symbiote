"""Write-origin provenance — ContextVar separating foreground vs agent-background writes.

Used by ``SkillsStore`` (and any future stateful tool the LLM can invoke) to
distinguish writes the user explicitly asked for ("foreground") from writes
the agent decided to make on its own during a self-improvement background
review ("background_review"). Only the latter are eligible for the future
SkillCuratorPhase to archive/consolidate. Skills the user wrote belong to
the user and the curator must never touch them.

Mirrors ``tools/skill_provenance.py`` from Hermes
(``~/dev/research/hermes-agent/tools/skill_provenance.py``).

Usage::

    from symbiote.core.provenance import (
        BACKGROUND_REVIEW,
        set_current_write_origin,
        reset_current_write_origin,
        is_background_review,
    )

    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        # any SkillsStore.create() call here is tagged agent_created=true
        ...
    finally:
        reset_current_write_origin(token)
"""

from __future__ import annotations

import contextvars

FOREGROUND = "foreground"
BACKGROUND_REVIEW = "background_review"

_write_origin: contextvars.ContextVar[str] = contextvars.ContextVar(
    "write_origin",
    default=FOREGROUND,
)


def set_current_write_origin(origin: str) -> contextvars.Token[str]:
    """Bind the active write origin to the current context.

    Returns a Token the caller MUST pass to ``reset_current_write_origin``
    in a finally block to avoid leaking the origin into the next task.
    """
    return _write_origin.set(origin or FOREGROUND)


def reset_current_write_origin(token: contextvars.Token[str]) -> None:
    """Restore the prior write origin."""
    _write_origin.reset(token)


def get_current_write_origin() -> str:
    """Return the active write origin.

    Default is ``FOREGROUND`` — any tool call made by a regular (non-review)
    agent, the CLI, or a host integration. ``BACKGROUND_REVIEW`` indicates
    the self-improvement review fork; only skills created under this origin
    are marked ``agent_created=true`` and eligible for curator management.
    """
    return _write_origin.get()


def is_background_review() -> bool:
    """Convenience: True iff the current write origin is the background review fork."""
    return get_current_write_origin() == BACKGROUND_REVIEW


__all__ = [
    "BACKGROUND_REVIEW",
    "FOREGROUND",
    "get_current_write_origin",
    "is_background_review",
    "reset_current_write_origin",
    "set_current_write_origin",
]
