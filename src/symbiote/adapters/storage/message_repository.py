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
        Each dict carries ``role``, ``content`` and ``created_at``.
        """
        if since is None:
            rows = self._storage.fetch_all(
                "SELECT role, content, created_at FROM messages "
                "WHERE session_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (session_id, limit),
            )
        else:
            rows = self._storage.fetch_all(
                "SELECT role, content, created_at FROM messages "
                "WHERE session_id = ? AND created_at > ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (session_id, since, limit),
            )
        return [
            {"role": r["role"], "content": r["content"], "created_at": r["created_at"]}
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
