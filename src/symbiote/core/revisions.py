"""Named, immutable revisions of a symbiote's configuration.

A host that keeps changing a symbiote while people are talking to it has no
safe way to do it: a change to the persona, the tools or the configuration
applies at once to every conversation, including the ones in the middle.

``kernel.create_revision(symbiote_id, label)`` takes a picture of what is
configured now: the persona (name, role, persona), the ``EnvironmentConfig``
with its list of tools, and the knowledge entries. A session started with
``revision=label`` is pinned to it: the context of its turns, the tool policy
and the knowledge search read the revision, whatever is edited afterwards. A
session without a revision reads the current configuration, as always.

What a revision does not hold stays the symbiote's own and is shared by every
session: memories, skills, the people it talks to (``subject_key``), the
harness texts, and the definitions of the tools (a revision fixes which
tools may be called, not what an HTTP tool's URL is). What happens after a
session closes (reflection, summary, dream) follows the current
configuration too.

A symbiote may have one default revision (``set_default_revision``). It is
only where a delegation lands: a pinned session that delegates to a symbiote
without a revision of the same label gets that symbiote's default instead of
whatever is being edited there. A session started without a revision still
reads the current configuration.

``active_revision_for(symbiote_id)`` is where the three readers
(``EnvironmentManager.get_config``, ``IdentityManager.get``,
``KnowledgeService.query``) look: it is bound for the duration of a turn.
"""

from __future__ import annotations

import contextvars
import json
import threading
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from symbiote.core.exceptions import (
    DuplicateEntityError,
    EntityNotFoundError,
    ValidationError,
)
from symbiote.core.ports import StoragePort

MAX_LABEL_CHARS = 100


class Revision(BaseModel):
    """A picture of a symbiote's configuration, taken once and never changed."""

    id: str
    symbiote_id: str
    label: str
    note: str | None = None
    # The persona as it was.
    name: str
    role: str
    persona: dict[str, Any] = Field(default_factory=dict)
    # The EnvironmentConfig as it was (None: the symbiote had none).
    config: dict[str, Any] | None = None
    knowledge_count: int = 0
    created_at: str
    # Set by ``retire_revision``: no new session may start on it.
    retired_at: str | None = None
    # The symbiote's default revision: what it answers with when a pinned
    # session of another symbiote delegates to it and it has no revision of
    # the same label (``set_default_revision``).
    is_default: bool = False


def validate_label(label: Any) -> str:
    """A revision label: a non-empty text of at most ``MAX_LABEL_CHARS``."""
    if not isinstance(label, str) or not label.strip():
        raise ValidationError("revision label must be a non-empty string")
    label = label.strip()
    if len(label) > MAX_LABEL_CHARS:
        raise ValidationError(f"revision label is longer than {MAX_LABEL_CHARS} characters")
    return label


# ── the revision of the turn in progress ────────────────────────────────

_active: contextvars.ContextVar[Revision | None] = contextvars.ContextVar(
    "symbiote_active_revision", default=None
)


def current_revision() -> Revision | None:
    """The revision the turn in progress is pinned to, if any."""
    return _active.get()


def active_revision_for(symbiote_id: str) -> Revision | None:
    """The revision that governs reads about ``symbiote_id`` right now.

    Only the symbiote the pinned session belongs to: a symbiote it delegates
    to is governed by the revision of its own sub-session.
    """
    revision = _active.get()
    if revision is not None and revision.symbiote_id == symbiote_id:
        return revision
    return None


def bind_revision(revision: Revision | None) -> contextvars.Token:
    """Make ``revision`` (or none) the one of the current turn."""
    return _active.set(revision)


def reset_revision(token: contextvars.Token) -> None:
    _active.reset(token)


# ── storage ─────────────────────────────────────────────────────────────


class RevisionStore:
    """Reads and writes ``symbiote_revisions`` and ``revision_knowledge``."""

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage
        # Revisions never change (retiring one only matters when a session
        # starts, which reads the database), so they are kept once loaded.
        self._cache: dict[tuple[str, str], Revision] = {}
        self._lock = threading.Lock()

    def create(
        self,
        *,
        symbiote_id: str,
        label: str,
        note: str | None,
        name: str,
        role: str,
        persona: dict[str, Any],
        config: dict[str, Any] | None,
        knowledge: list[Any],
    ) -> Revision:
        """Store the picture; raises ``DuplicateEntityError`` when the
        symbiote already has a revision with this label."""
        label = validate_label(label)
        if self._fetch(symbiote_id, label) is not None:
            raise DuplicateEntityError("Revision", label)
        revision_id = str(uuid4())
        now = datetime.now(tz=UTC).isoformat()
        with self._storage.transaction():
            self._storage.execute(
                "INSERT INTO symbiote_revisions "
                "(id, symbiote_id, label, note, name, role, persona_json, config_json, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision_id, symbiote_id, label, note, name, role,
                    json.dumps(persona, default=str),
                    json.dumps(config, default=str) if config is not None else None,
                    now,
                ),
            )
            for entry in knowledge:
                self._storage.execute(
                    "INSERT INTO revision_knowledge "
                    "(id, revision_id, entry_id, symbiote_id, name, source_path, content, "
                    "type, tags_json, created_at, auto_recall) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid4()), revision_id, entry.id, symbiote_id, entry.name,
                        entry.source_path, entry.content, entry.type,
                        json.dumps(entry.tags), entry.created_at.isoformat(),
                        int(entry.auto_recall),
                    ),
                )
        found = self.get(symbiote_id, label)
        assert found is not None
        return found

    def get(self, symbiote_id: str, label: str) -> Revision | None:
        """The revision, from memory when already loaded."""
        key = (symbiote_id, label)
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached
        revision = self._fetch(symbiote_id, label)
        if revision is not None:
            with self._lock:
                self._cache[key] = revision
        return revision

    def require_open(self, symbiote_id: str, label: str) -> Revision:
        """The revision a new session may start on: it exists and was not
        retired (read from the database, not from memory)."""
        label = validate_label(label)
        revision = self._fetch(symbiote_id, label)
        if revision is None:
            raise EntityNotFoundError("Revision", label)
        if revision.retired_at is not None:
            raise ValidationError(
                f"revision {label!r} was retired: no new session may start on it"
            )
        return revision

    def fetch(self, symbiote_id: str, label: str) -> Revision | None:
        """The revision as the database has it now (``retired_at`` and
        ``is_default`` change; the rest never does)."""
        return self._fetch(symbiote_id, label)

    def default_for(self, symbiote_id: str) -> Revision | None:
        """The symbiote's default revision, or ``None`` (read from the
        database: another replica may have moved it)."""
        row = self._storage.fetch_one(
            f"{self._SELECT} WHERE r.symbiote_id = ? AND r.is_default = 1 "
            "AND r.retired_at IS NULL ORDER BY r.created_at DESC, r.rowid DESC",
            (symbiote_id,),
        )
        return self._to_model(row) if row is not None else None

    def set_default(self, symbiote_id: str, label: str | None) -> Revision | None:
        """Make ``label`` the symbiote's default revision, or leave it with
        none (``None``). Raises as ``require_open`` does."""
        revision = self.require_open(symbiote_id, label) if label is not None else None
        with self._storage.transaction():
            self._storage.execute(
                "UPDATE symbiote_revisions SET is_default = 0 "
                "WHERE symbiote_id = ? AND is_default = 1",
                (symbiote_id,),
            )
            if revision is not None:
                self._storage.execute(
                    "UPDATE symbiote_revisions SET is_default = 1 "
                    "WHERE symbiote_id = ? AND label = ?",
                    (symbiote_id, revision.label),
                )
        self.forget(symbiote_id)
        return self._fetch(symbiote_id, revision.label) if revision is not None else None

    def list_for(self, symbiote_id: str) -> list[Revision]:
        rows = self._storage.fetch_all(
            f"{self._SELECT} WHERE r.symbiote_id = ? ORDER BY r.created_at DESC, r.rowid DESC",
            (symbiote_id,),
        )
        return [self._to_model(r) for r in rows]

    def retire(self, symbiote_id: str, label: str) -> Revision:
        """No new session may start on the revision; the pinned ones go on."""
        label = validate_label(label)
        current = self._fetch(symbiote_id, label)
        if current is None:
            raise EntityNotFoundError("Revision", label)
        if current.is_default and current.retired_at is None:
            raise ValidationError(
                f"revision {label!r} is the symbiote's default: move the default "
                "to another revision (or to none) before retiring it"
            )
        if current.retired_at is None:
            self._storage.execute(
                "UPDATE symbiote_revisions SET retired_at = ? "
                "WHERE symbiote_id = ? AND label = ?",
                (datetime.now(tz=UTC).isoformat(), symbiote_id, label),
            )
        with self._lock:
            self._cache.pop((symbiote_id, label), None)
        found = self._fetch(symbiote_id, label)
        assert found is not None
        return found

    def forget(self, symbiote_id: str) -> None:
        """Drop what is in memory about a symbiote (it was purged)."""
        with self._lock:
            for key in [k for k in self._cache if k[0] == symbiote_id]:
                del self._cache[key]

    _SELECT = (
        "SELECT r.id, r.symbiote_id, r.label, r.note, r.name, r.role, r.persona_json, "
        "r.config_json, r.created_at, r.retired_at, r.is_default, "
        "(SELECT COUNT(*) FROM revision_knowledge k WHERE k.revision_id = r.id) "
        "AS knowledge_count FROM symbiote_revisions r"
    )

    def _fetch(self, symbiote_id: str, label: str) -> Revision | None:
        row = self._storage.fetch_one(
            f"{self._SELECT} WHERE r.symbiote_id = ? AND r.label = ?", (symbiote_id, label)
        )
        return self._to_model(row) if row is not None else None

    @staticmethod
    def _to_model(row: dict) -> Revision:
        config = row.get("config_json")
        return Revision(
            id=row["id"],
            symbiote_id=row["symbiote_id"],
            label=row["label"],
            note=row.get("note"),
            name=row.get("name") or "",
            role=row.get("role") or "",
            persona=json.loads(row.get("persona_json") or "{}"),
            config=json.loads(config) if config else None,
            knowledge_count=int(row.get("knowledge_count") or 0),
            created_at=row["created_at"],
            retired_at=row.get("retired_at"),
            is_default=bool(row.get("is_default") or 0),
        )


__all__ = [
    "MAX_LABEL_CHARS",
    "Revision",
    "RevisionStore",
    "active_revision_for",
    "bind_revision",
    "current_revision",
    "reset_revision",
    "validate_label",
]
