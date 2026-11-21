"""Session management — create, resume, close sessions and track messages/decisions."""

from __future__ import annotations

import json
import threading
from datetime import datetime
from typing import Literal

from symbiote.core.exceptions import (
    EntityNotFoundError,
    StorageIntegrityError,
    ValidationError,
)
from symbiote.core.models import Decision, Message, Session, _utcnow
from symbiote.core.origin import normalize_origin
from symbiote.core.ports import StoragePort
from symbiote.core.session_scope import (
    DELEGATION_OPEN,
    tools_from_json,
    tools_to_json,
    validate_delegation,
    validate_tools_allowed,
)
from symbiote.core.subjects import validate_subject_key

# ``close(summary=...)`` not given: generate the summary as always.
_GENERATE: object = object()

_key_locks: dict[tuple[str, str], threading.Lock] = {}
_key_locks_guard = threading.Lock()


def _external_key_lock(symbiote_id: str, external_key: str) -> threading.Lock:
    """Process-wide lock for one (symbiote, external_key) pair. Shared by all
    SessionManager instances (the HTTP API builds one per request)."""
    with _key_locks_guard:
        return _key_locks.setdefault((symbiote_id, external_key), threading.Lock())


class SessionManager:
    """Manages session lifecycle, messages and decisions."""

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage

    # ── Session lifecycle ──────────────────────────────────────────────

    def start(
        self,
        symbiote_id: str,
        goal: str | None = None,
        workspace_id: str | None = None,
        external_key: str | None = None,
        subject_key: str | None = None,
        revision: str | None = None,
        tools_allowed: list[str] | None = None,
        delegation: str | None = None,
    ) -> Session:
        """Create a new active session and persist it.

        ``subject_key`` names the end user the session belongs to (see
        ``symbiote.core.subjects``); it cannot be changed afterwards. Raises
        ``ValidationError`` when it is empty or too long. ``revision`` is the
        label of the revision the session is pinned to (the caller checked
        that it exists). ``tools_allowed`` and ``delegation`` are what the
        session narrows for itself (see ``symbiote.core.session_scope``).
        """
        session = Session(
            symbiote_id=symbiote_id,
            goal=goal,
            workspace_id=workspace_id,
            external_key=external_key,
            subject_key=validate_subject_key(subject_key),
            revision=revision,
            tools_allowed=validate_tools_allowed(tools_allowed),
            delegation=validate_delegation(delegation),
            status="active",
        )
        self._storage.execute(
            "INSERT INTO sessions "
            "(id, symbiote_id, goal, workspace_id, external_key, subject_key, "
            "status, started_at, ended_at, summary, revision, tools_allowed, delegation) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session.id,
                session.symbiote_id,
                session.goal,
                session.workspace_id,
                session.external_key,
                session.subject_key,
                session.status,
                session.started_at.isoformat(),
                None,
                None,
                session.revision,
                tools_to_json(session.tools_allowed),
                session.delegation,
            ),
        )
        return session

    def set_tools(self, session_id: str, tools_allowed: list[str] | None) -> Session:
        """Narrow the session to these tools, or lift the restriction
        (``None``). Raises EntityNotFoundError if not found."""
        self._storage.execute(
            "UPDATE sessions SET tools_allowed = ? WHERE id = ?",
            (tools_to_json(validate_tools_allowed(tools_allowed)), session_id),
        )
        return self._require(session_id)

    def set_delegation(self, session_id: str, delegation: str | None) -> Session:
        """Set the session's delegation mode (``None``: ``"open"``). Raises
        EntityNotFoundError if not found."""
        self._storage.execute(
            "UPDATE sessions SET delegation = ? WHERE id = ?",
            (validate_delegation(delegation), session_id),
        )
        return self._require(session_id)

    def _require(self, session_id: str) -> Session:
        session = self.get(session_id)
        if session is None:
            raise EntityNotFoundError("Session", session_id)
        return session

    def set_revision(self, session_id: str, revision: str | None) -> Session:
        """Pin the session to another revision, or to none (``None``: the
        current configuration). Raises EntityNotFoundError if not found."""
        self._storage.execute(
            "UPDATE sessions SET revision = ? WHERE id = ?", (revision, session_id)
        )
        return self._require(session_id)

    def get_or_create_by_external_key(
        self,
        symbiote_id: str,
        external_key: str,
        goal: str | None = None,
        subject_key: str | None = None,
        revision: str | None = None,
        tools_allowed: list[str] | None = None,
        delegation: str | None = None,
    ) -> Session:
        """Find an active session by external_key, or create one.

        Concurrent calls for the same (symbiote, key) get the same session: a
        process-wide lock per key serializes find-or-create, and the unique
        index on (symbiote_id, external_key) covers other processes.

        ``subject_key`` is given to a session created here. A session that
        already exists keeps its own: asking for it with a different
        ``subject_key`` raises ``ValidationError``, because the conversation
        of one end user must never continue as another's. Without
        ``subject_key`` the existing session is returned as it is.

        ``revision`` is given to a session created here. A session that
        already exists keeps the revision it has (``Session.revision`` says
        which): a conversation in progress does not change configuration
        because a newer revision was published. ``set_revision`` moves it.

        ``tools_allowed`` and ``delegation`` follow the same rule: given to
        a session created here, kept as they are by one that already exists
        (``set_tools`` and ``set_delegation`` change them).
        """
        subject_key = validate_subject_key(subject_key)
        tools_allowed = validate_tools_allowed(tools_allowed)
        delegation = validate_delegation(delegation)
        with _external_key_lock(symbiote_id, external_key):
            try:
                return self._get_or_create_by_external_key(
                    symbiote_id, external_key, goal, subject_key, revision,
                    tools_allowed, delegation,
                )
            except StorageIntegrityError:
                # Another process created it between our SELECT and INSERT.
                return self._get_or_create_by_external_key(
                    symbiote_id, external_key, goal, subject_key, revision,
                    tools_allowed, delegation,
                )

    def _get_or_create_by_external_key(
        self,
        symbiote_id: str,
        external_key: str,
        goal: str | None,
        subject_key: str | None = None,
        revision: str | None = None,
        tools_allowed: list[str] | None = None,
        delegation: str | None = None,
    ) -> Session:
        row = self._storage.fetch_one(
            "SELECT * FROM sessions WHERE external_key = ? AND symbiote_id = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (external_key, symbiote_id),
        )
        if row is not None:
            session = self._row_to_session(row)
            if subject_key is not None and session.subject_key != subject_key:
                raise ValidationError(
                    f"session with external_key {external_key!r} belongs to "
                    "another subject_key (or to none); a session's subject "
                    "cannot change"
                )
            if session.status == "closed":
                self._storage.execute(
                    "UPDATE sessions SET status = 'active', ended_at = NULL WHERE id = ?",
                    (session.id,),
                )
                session = session.model_copy(
                    update={"status": "active", "ended_at": None}
                )
            return session
        return self.start(
            symbiote_id=symbiote_id,
            goal=goal,
            external_key=external_key,
            subject_key=subject_key,
            revision=revision,
            tools_allowed=tools_allowed,
            delegation=delegation,
        )

    def find_by_external_key(self, external_key: str) -> Session | None:
        """Find the most recent session with the given external_key."""
        row = self._storage.fetch_one(
            "SELECT * FROM sessions WHERE external_key = ? ORDER BY started_at DESC LIMIT 1",
            (external_key,),
        )
        if row is None:
            return None
        return self._row_to_session(row)

    def get(self, session_id: str) -> Session | None:
        """Fetch a session by id without changing it."""
        row = self._storage.fetch_one(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )
        return self._row_to_session(row) if row is not None else None

    def resume(self, session_id: str) -> Session | None:
        """Fetch a session by id. Reopen it if closed. Return None if not found."""
        row = self._storage.fetch_one(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )
        if row is None:
            return None

        session = self._row_to_session(row)

        if session.status == "closed":
            self._storage.execute(
                "UPDATE sessions SET status = 'active', ended_at = NULL WHERE id = ?",
                (session_id,),
            )
            session = session.model_copy(
                update={"status": "active", "ended_at": None}
            )

        return session

    def set_learn(self, session_id: str, learn: bool) -> None:
        """Mark whether the symbiote learns from the session (``Session.learn``)."""
        self._storage.execute(
            "UPDATE sessions SET learn = ? WHERE id = ?", (int(learn), session_id)
        )

    def close(
        self,
        session_id: str,
        *,
        summary: str | None | object = _GENERATE,
        learn: bool = True,
    ) -> Session:
        """Close a session — set status, ended_at, and generate summary.

        ``summary`` replaces the generated one: a text written by the caller,
        or ``None`` to store no summary. ``learn`` is stored as the session's
        mark (``Session.learn``).
        """
        row = self._storage.fetch_one(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )
        if row is None:
            raise EntityNotFoundError("Session", session_id)

        now = _utcnow()
        if summary is _GENERATE:
            summary = self._generate_summary(session_id)

        self._storage.execute(
            "UPDATE sessions SET status = 'closed', ended_at = ?, summary = ?, learn = ? "
            "WHERE id = ?",
            (now.isoformat(), summary, int(learn), session_id),
        )

        session = self._row_to_session(row)
        return session.model_copy(
            update={
                "status": "closed",
                "ended_at": now,
                "summary": summary,
                "learn": learn,
            }
        )

    # ── Messages ───────────────────────────────────────────────────────

    def add_message(
        self,
        session_id: str,
        role: Literal["user", "assistant", "system"],
        content: str,
        *,
        origin: str | None = None,
    ) -> Message:
        """Persist a message linked to the given session.

        ``origin`` names who wrote an input that the person did not write
        (``symbiote.core.origin``); ``None``, the default, is the person.
        """
        self._assert_session_exists(session_id)

        msg = Message(
            session_id=session_id,
            role=role,
            content=content,
            origin=normalize_origin(origin),
        )
        self._storage.execute(
            "INSERT INTO messages (id, session_id, role, content, created_at, origin) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                msg.id,
                msg.session_id,
                msg.role,
                msg.content,
                msg.created_at.isoformat(),
                msg.origin,
            ),
        )
        return msg

    def delete_message(self, message_id: str) -> None:
        """Remove one message (used to undo the user message of a failed turn)."""
        self._storage.execute("DELETE FROM messages WHERE id = ?", (message_id,))

    def get_messages(self, session_id: str, limit: int = 50) -> list[Message]:
        """Return messages for a session, most recent first."""
        rows = self._storage.fetch_all(
            "SELECT * FROM messages WHERE session_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (session_id, limit),
        )
        return [self._row_to_message(r) for r in rows]

    # ── Decisions ──────────────────────────────────────────────────────

    def add_decision(
        self,
        session_id: str,
        title: str,
        description: str | None = None,
        tags: list[str] | None = None,
    ) -> Decision:
        """Persist a decision linked to the given session."""
        self._assert_session_exists(session_id)

        dec = Decision(
            session_id=session_id,
            title=title,
            description=description,
            tags=tags or [],
        )
        self._storage.execute(
            "INSERT INTO decisions (id, session_id, title, description, tags_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                dec.id,
                dec.session_id,
                dec.title,
                dec.description,
                json.dumps(dec.tags),
                dec.created_at.isoformat(),
            ),
        )
        return dec

    def get_decisions(self, session_id: str) -> list[Decision]:
        """Return all decisions for a session."""
        rows = self._storage.fetch_all(
            "SELECT * FROM decisions WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        )
        return [self._row_to_decision(r) for r in rows]

    # ── Private helpers ────────────────────────────────────────────────

    def _assert_session_exists(self, session_id: str) -> None:
        row = self._storage.fetch_one(
            "SELECT id FROM sessions WHERE id = ?", (session_id,)
        )
        if row is None:
            raise EntityNotFoundError("Session", session_id)

    def _generate_summary(self, session_id: str) -> str:
        """Concatenate content of last 5 messages, or 'No messages'."""
        rows = self._storage.fetch_all(
            "SELECT content FROM messages WHERE session_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 5",
            (session_id,),
        )
        if not rows:
            return "No messages"
        # Reverse so they read chronologically, then join
        contents = [r["content"] for r in reversed(rows)]
        return "\n".join(contents)

    @staticmethod
    def _row_to_session(row: dict) -> Session:
        ended_at = (
            datetime.fromisoformat(row["ended_at"]) if row.get("ended_at") else None
        )
        return Session(
            id=row["id"],
            symbiote_id=row["symbiote_id"],
            goal=row.get("goal"),
            workspace_id=row.get("workspace_id"),
            external_key=row.get("external_key"),
            subject_key=row.get("subject_key"),
            revision=row.get("revision"),
            tools_allowed=tools_from_json(row.get("tools_allowed")),
            delegation=row.get("delegation") or DELEGATION_OPEN,
            status=row["status"],
            started_at=datetime.fromisoformat(row["started_at"]),
            ended_at=ended_at,
            summary=row.get("summary"),
            learn=row.get("learn") != 0,
        )

    @staticmethod
    def _row_to_message(row: dict) -> Message:
        return Message(
            id=row["id"],
            session_id=row["session_id"],
            role=row["role"],
            content=row["content"],
            origin=row.get("origin"),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _row_to_decision(row: dict) -> Decision:
        tags = json.loads(row.get("tags_json") or "[]")
        return Decision(
            id=row["id"],
            session_id=row["session_id"],
            title=row["title"],
            description=row.get("description"),
            tags=tags,
            created_at=datetime.fromisoformat(row["created_at"]),
        )
