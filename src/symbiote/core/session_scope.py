"""What one session narrows for itself: its tools and its delegation.

The same symbiote often serves different audiences (its owner, signed-in
customers, anonymous visitors), each allowed a different set of tools. A
session may carry a list of the tools it can use, ``tools_allowed``:

- the session sees and may call only the tools that are both in that list and
  authorized by the configuration it reads (the current one, or the revision
  it is pinned to). The list only narrows: a tool the configuration does not
  authorize stays refused whatever the list says;
- it covers every tool, including the ones a mode adds by itself
  (``mode_tools``): a tool that is not in the list is not shown to the model
  and is refused if asked for;
- it follows the delegation: the session of a symbiote this one delegates to
  (``spawn``) carries the same list, so it bounds what the conversation can
  make happen anywhere in the chain. ``spawn`` itself must be in the list for
  the delegation to happen at all.

``delegation`` says what a delegated symbiote may answer with: ``"open"``
(the default) lets it fall back to its current configuration, ``"strict"``
only lets it answer from a revision (see ``SubagentManager``).

The scope is bound for the duration of a turn; ``PolicyGate.check`` and the
tool listing read it.
"""

from __future__ import annotations

import contextvars
import json
from dataclasses import dataclass
from typing import Any

from symbiote.core.exceptions import ValidationError

DELEGATION_OPEN = "open"
DELEGATION_STRICT = "strict"
DELEGATION_MODES = (DELEGATION_OPEN, DELEGATION_STRICT)

MAX_TOOLS_ALLOWED = 1000
MAX_TOOL_ID_CHARS = 200


@dataclass(frozen=True)
class SessionScope:
    """What the session of the turn in progress narrows."""

    symbiote_id: str
    session_id: str
    # None: no restriction. An empty set: no tool at all.
    tools_allowed: frozenset[str] | None = None
    delegation: str = DELEGATION_OPEN


def validate_tools_allowed(value: Any) -> list[str] | None:
    """A session's list of tools: ``None`` (no restriction) or a list of tool
    ids, without repeats, in the order given. An empty list means no tool."""
    if value is None:
        return None
    if isinstance(value, str) or not isinstance(value, (list, tuple, set, frozenset)):
        raise ValidationError("tools_allowed must be a list of tool ids, or None")
    tools: list[str] = []
    for item in sorted(value) if isinstance(value, (set, frozenset)) else value:
        if not isinstance(item, str) or not item.strip():
            raise ValidationError("tools_allowed must hold non-empty tool ids")
        tool_id = item.strip()
        if len(tool_id) > MAX_TOOL_ID_CHARS:
            raise ValidationError(
                f"a tool id in tools_allowed is longer than {MAX_TOOL_ID_CHARS} characters"
            )
        if tool_id not in tools:
            tools.append(tool_id)
    if len(tools) > MAX_TOOLS_ALLOWED:
        raise ValidationError(f"tools_allowed holds more than {MAX_TOOLS_ALLOWED} tool ids")
    return tools


def tools_to_json(tools: list[str] | None) -> str | None:
    """How ``sessions.tools_allowed`` stores the list (NULL: no restriction)."""
    return json.dumps(tools) if tools is not None else None


def tools_from_json(raw: str | None) -> list[str] | None:
    """Read ``sessions.tools_allowed``. A value that cannot be read means no
    tool (fail closed), never "no restriction"."""
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    return [str(t) for t in value] if isinstance(value, list) else []


def validate_delegation(value: Any) -> str:
    """A session's delegation mode; ``None`` is ``"open"``."""
    if value is None:
        return DELEGATION_OPEN
    if value not in DELEGATION_MODES:
        raise ValidationError(
            f"delegation must be one of {list(DELEGATION_MODES)}, got {value!r}"
        )
    return str(value)


# ── the scope of the turn in progress ───────────────────────────────────

_scope: contextvars.ContextVar[SessionScope | None] = contextvars.ContextVar(
    "symbiote_session_scope", default=None
)


def current_session_scope() -> SessionScope | None:
    """The scope of the session whose turn is in progress, if any."""
    return _scope.get()


def session_tools_for(symbiote_id: str) -> frozenset[str] | None:
    """The tools the session in progress allows ``symbiote_id``, or ``None``
    when nothing is narrowed.

    Only the symbiote the session belongs to: a symbiote it delegates to is
    governed by the scope of its own sub-session, which carries the same
    list.
    """
    scope = _scope.get()
    if scope is not None and scope.symbiote_id == symbiote_id:
        return scope.tools_allowed
    return None


def tool_in_session(symbiote_id: str, tool_id: str) -> bool:
    """False when the session in progress narrows its tools and ``tool_id``
    is not among them."""
    allowed = session_tools_for(symbiote_id)
    return allowed is None or tool_id in allowed


def bind_session_scope(scope: SessionScope | None) -> contextvars.Token:
    """Make ``scope`` (or none) the one of the current turn."""
    return _scope.set(scope)


def reset_session_scope(token: contextvars.Token) -> None:
    _scope.reset(token)


__all__ = [
    "DELEGATION_MODES",
    "DELEGATION_OPEN",
    "DELEGATION_STRICT",
    "SessionScope",
    "bind_session_scope",
    "current_session_scope",
    "reset_session_scope",
    "session_tools_for",
    "tool_in_session",
    "tools_from_json",
    "tools_to_json",
    "validate_delegation",
    "validate_tools_allowed",
]
