"""MessageRepository — implements MessagePort to isolate SQL from consumers."""

from __future__ import annotations

from symbiote.core.ports import StoragePort


class MessageRepository:
    """Concrete implementation of MessagePort backed by StoragePort."""

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage

    def get_messages(
        self, session_id: str, limit: int = 50
    ) -> list[dict]:
        """Return messages for a session, chronological order."""
        rows = self._storage.fetch_all(
            "SELECT role, content FROM messages "
            "WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
            (session_id, limit),
        )
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
