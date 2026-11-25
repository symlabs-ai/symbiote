"""MessageRepository — implements MessagePort to isolate SQL from consumers."""

from __future__ import annotations

from symbiote.core.ports import StoragePort


class MessageRepository:
    """Concrete implementation of MessagePort backed by StoragePort."""

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage

    def get_messages(
        self, session_id: str, limit: int = 50, *, since: str | None = None
    ) -> list[dict]:
        """Return messages for a session, chronological order.

        With ``since`` (an ISO timestamp), only messages created after it.
        Each dict carries ``role``, ``content``, ``created_at``, ``origin``
        (``None`` unless the message is an input that the person did not write,
        see ``symbiote.core.origin``) and ``idempotency_key`` (normally None).
        """
        if since is None:
            rows = self._storage.fetch_all(
                "SELECT m.role, m.content, m.created_at, m.origin, a.idempotency_key "
                "FROM messages m LEFT JOIN message_appends a ON a.message_id = m.id "
                "WHERE m.session_id = ? ORDER BY m.created_at DESC, m.rowid DESC LIMIT ?",
                (session_id, limit),
            )
        else:
            rows = self._storage.fetch_all(
                "SELECT m.role, m.content, m.created_at, m.origin, a.idempotency_key "
                "FROM messages m LEFT JOIN message_appends a ON a.message_id = m.id "
                "WHERE m.session_id = ? AND m.created_at > ? "
                "ORDER BY m.created_at DESC, m.rowid DESC LIMIT ?",
                (session_id, since, limit),
            )
        return [
            {
                "role": r["role"],
                "content": r["content"],
                "created_at": r["created_at"],
                "origin": r.get("origin"),
                "idempotency_key": r.get("idempotency_key"),
            }
            for r in reversed(rows)
        ]

    def get_reflection_watermark(self, session_id: str) -> str | None:
        """created_at of the last message already reflected for the session."""
        row = self._storage.fetch_one(
            "SELECT reflected_until FROM sessions WHERE id = ?", (session_id,)
        )
        return row["reflected_until"] if row else None

    def set_reflection_watermark(self, session_id: str, created_at: str) -> None:
        self._storage.execute(
            "UPDATE sessions SET reflected_until = ? WHERE id = ?",
            (created_at, session_id),
        )
