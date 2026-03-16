"""KnowledgeService — register, query, and manage knowledge entries."""

from __future__ import annotations

import json
from datetime import datetime

from symbiote.core.exceptions import EntityNotFoundError
from symbiote.core.ports import StoragePort
from symbiote.knowledge.models import KnowledgeEntry


class KnowledgeService:
    """Manages knowledge entry CRUD and search."""

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage

    # ── public API ─────────────────────────────────────────────────────

    def register_source(
        self,
        symbiote_id: str,
        name: str,
        source_path: str | None = None,
        content: str | None = None,
        entry_type: str = "document",
        tags: list[str] | None = None,
    ) -> KnowledgeEntry:
        """Create and persist a knowledge entry."""
        entry = KnowledgeEntry(
            symbiote_id=symbiote_id,
            name=name,
            source_path=source_path,
            content=content,
            type=entry_type,  # type: ignore[arg-type]
            tags=tags or [],
        )
        self._storage.execute(
            "INSERT INTO knowledge_entries "
            "(id, symbiote_id, name, source_path, content, type, tags_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.id,
                entry.symbiote_id,
                entry.name,
                entry.source_path,
                entry.content,
                entry.type,
                json.dumps(entry.tags),
                entry.created_at.isoformat(),
            ),
        )
        return entry

    def get(self, entry_id: str) -> KnowledgeEntry | None:
        """Fetch a knowledge entry by ID, returning None if not found."""
        row = self._storage.fetch_one(
            "SELECT * FROM knowledge_entries WHERE id = ?", (entry_id,)
        )
        if row is None:
            return None
        return self._row_to_entry(row)

    def query(
        self, symbiote_id: str, theme: str, limit: int = 10
    ) -> list[KnowledgeEntry]:
        """Search entries by content/name matching theme, filtered by symbiote_id."""
        pattern = f"%{theme}%"
        rows = self._storage.fetch_all(
            "SELECT * FROM knowledge_entries "
            "WHERE symbiote_id = ? AND (name LIKE ? OR content LIKE ?) "
            "ORDER BY created_at DESC LIMIT ?",
            (symbiote_id, pattern, pattern, limit),
        )
        return [self._row_to_entry(r) for r in rows]

    def list_by_symbiote(self, symbiote_id: str) -> list[KnowledgeEntry]:
        """Return all knowledge entries for a symbiote."""
        rows = self._storage.fetch_all(
            "SELECT * FROM knowledge_entries WHERE symbiote_id = ?",
            (symbiote_id,),
        )
        return [self._row_to_entry(r) for r in rows]

    def remove(self, entry_id: str) -> None:
        """Delete a knowledge entry. Raises EntityNotFoundError if not found."""
        existing = self.get(entry_id)
        if existing is None:
            raise EntityNotFoundError("KnowledgeEntry", entry_id)
        self._storage.execute(
            "DELETE FROM knowledge_entries WHERE id = ?", (entry_id,)
        )

    # ── private helpers ────────────────────────────────────────────────

    @staticmethod
    def _row_to_entry(row: dict) -> KnowledgeEntry:
        created = row["created_at"]
        if isinstance(created, str):
            created = datetime.fromisoformat(created)
        tags = row.get("tags_json", "[]")
        if isinstance(tags, str):
            tags = json.loads(tags)
        return KnowledgeEntry(
            id=row["id"],
            symbiote_id=row["symbiote_id"],
            name=row["name"],
            source_path=row.get("source_path"),
            content=row.get("content"),
            type=row.get("type", "document"),
            tags=tags,
            created_at=created,
        )
