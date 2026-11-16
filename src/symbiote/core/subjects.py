"""End users of a symbiote (``subject_key``).

A symbiote that talks to several people keeps one memory partition per
person. The host names the person with an opaque ``subject_key`` when it
starts the session; the kernel only compares the value for equality.

The two rules, enforced by ``MemoryStore``:

- **Read**: a session with ``subject_key = S`` sees the shared memories
  (``subject_key IS NULL``) and the ones of ``S``. A session without a
  subject sees only the shared ones.
- **Write**: whatever is stored on behalf of a session with ``subject_key =
  S`` belongs to ``S``. Such a session never creates, changes or deactivates
  a shared memory or another subject's.

``NULL`` everywhere is the behaviour from before subjects existed.
"""

from __future__ import annotations

from typing import Any, Final

from symbiote.core.exceptions import ValidationError
from symbiote.core.ports import StoragePort

# Same limit as ``external_key`` in the HTTP API (``api.limits.MAX_KEY_CHARS``).
MAX_SUBJECT_KEY_CHARS = 500


class AnySubject:
    """Sentinel type of ``ANY_SUBJECT``."""

    _instance: AnySubject | None = None

    def __new__(cls) -> AnySubject:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "ANY_SUBJECT"


# "Whatever the partition": for administrative calls (the inspector, a host
# deactivating a memory by id) that are not acting for one session.
ANY_SUBJECT: Final = AnySubject()

# Sessions that belong to an end user.
_SUBJECT_SESSIONS = "SELECT id FROM sessions WHERE subject_key IS NOT NULL"


def validate_subject_key(value: Any) -> str | None:
    """Return ``value`` if it can name an end user; ``None`` stays ``None``.

    Raises ``ValidationError`` for a non-string, an empty or blank string, or
    one longer than ``MAX_SUBJECT_KEY_CHARS``.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(
            f"subject_key must be a string, got {type(value).__name__}"
        )
    if not value.strip():
        raise ValidationError("subject_key must not be empty")
    if len(value) > MAX_SUBJECT_KEY_CHARS:
        raise ValidationError(
            f"subject_key is longer than {MAX_SUBJECT_KEY_CHARS} characters"
        )
    return value


def subject_of_session(storage: StoragePort, session_id: str | None) -> str | None:
    """The ``subject_key`` of a session; ``None`` when it has none or does
    not exist."""
    if not session_id:
        return None
    row = storage.fetch_one(
        "SELECT subject_key FROM sessions WHERE id = ?", (session_id,)
    )
    value = row.get("subject_key") if row is not None else None
    return value if isinstance(value, str) and value else None


def subject_kwargs(memory: Any, session_id: str | None) -> dict[str, Any]:
    """The keyword to add to a memory read made for ``session_id``:
    ``{"subject_key": S}`` when the session belongs to end user ``S``, else
    ``{}``.

    Empty also for a memory port without ``subject_for_session`` (one
    written before subjects existed), which is then called exactly as
    before.
    """
    resolver = getattr(memory, "subject_for_session", None)
    if resolver is None:
        return {}
    subject = resolver(session_id)
    return {"subject_key": subject} if isinstance(subject, str) and subject else {}


def learns_from_subjects(storage: StoragePort, symbiote_id: str) -> bool:
    """``EnvironmentConfig.learn_from_subject_sessions`` of the symbiote
    (False when it has no configuration)."""
    row = storage.fetch_one(
        "SELECT learn_from_subject_sessions AS flag FROM environment_configs "
        "WHERE symbiote_id = ? AND workspace_id IS NULL",
        (symbiote_id,),
    )
    return bool(row and row.get("flag"))


def learning_sessions_clause(
    storage: StoragePort, symbiote_id: str, column: str = "session_id"
) -> str:
    """SQL condition for the readers that learn from a symbiote's sessions
    (parameter tuner, harness evolver, the dream's ``mine`` and ``evaluate``).

    Empty when the symbiote learns from its end users' sessions; otherwise
    `` AND <column> NOT IN (...)``, which leaves those sessions out.
    ``column`` comes from the caller's own SQL, never from user input.
    """
    if learns_from_subjects(storage, symbiote_id):
        return ""
    return f" AND {column} NOT IN ({_SUBJECT_SESSIONS})"


__all__ = [
    "ANY_SUBJECT",
    "AnySubject",
    "MAX_SUBJECT_KEY_CHARS",
    "learning_sessions_clause",
    "learns_from_subjects",
    "subject_kwargs",
    "subject_of_session",
    "validate_subject_key",
]
