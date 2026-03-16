"""Session management — create, resume, close sessions and track messages/decisions."""

from __future__ import annotations

import json
from datetime import datetime

from symbiote.core.exceptions import EntityNotFoundError
from symbiote.core.models import Decision, Message, Session, _utcnow
from symbiote.core.ports import StoragePort


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
    ) -> Session:
        """Create a new active session and persist it."""
        session = Session(
            symbiote_id=symbiote_id,
            goal=goal,
            workspace_id=workspace_id,
            status="active",
        )
        self._storage.execute(
            "INSERT INTO sessions (id, symbiote_id, goal, workspace_id, status, started_at, ended_at, summary) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session.id,
                session.symbiote_id,
                session.goal,
                session.workspace_id,
                session.status,
                session.started_at.isoformat(),
                None,
                None,
            ),
        )
        return session

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

    def close(self, session_id: str) -> Session:
        """Close a session — set status, ended_at, and generate summary."""
        row = self._storage.fetch_one(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )
        if row is None:
            raise EntityNotFoundError("Session", session_id)

        now = _utcnow()
        summary = self._generate_summary(session_id)

        self._storage.execute(
            "UPDATE sessions SET status = 'closed', ended_at = ?, summary = ? WHERE id = ?",
            (now.isoformat(), summary, session_id),
        )

        session = self._row_to_session(row)
        return session.model_copy(
            update={"status": "closed", "ended_at": now, "summary": summary}
        )

    # ── Messages ───────────────────────────────────────────────────────

    def add_message(self, session_id: str, role: str, content: str) -> Message:
        """Persist a message linked to the given session."""
        self._assert_session_exists(session_id)

        msg = Message(session_id=session_id, role=role, content=content)
        self._storage.execute(
            "INSERT INTO messages (id, session_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (msg.id, msg.session_id, msg.role, msg.content, msg.created_at.isoformat()),
        )
        return msg

    def get_messages(self, session_id: str, limit: int = 50) -> list[Message]:
        """Return messages for a session, most recent first."""
        rows = self._storage.fetch_all(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
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
            "SELECT content FROM messages WHERE session_id = ? ORDER BY created_at DESC LIMIT 5",
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
            status=row["status"],
            started_at=datetime.fromisoformat(row["started_at"]),
            ended_at=ended_at,
            summary=row.get("summary"),
        )

    @staticmethod
    def _row_to_message(row: dict) -> Message:
        return Message(
            id=row["id"],
            session_id=row["session_id"],
            role=row["role"],
            content=row["content"],
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
