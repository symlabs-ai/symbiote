"""KnowledgeService — register, query, and manage knowledge entries."""

from __future__ import annotations

import json
from datetime import datetime

from symbiote.core.exceptions import EntityNotFoundError
from symbiote.core.ports import StoragePort
from symbiote.core.sql import contains, like_contains
from symbiote.knowledge.models import KnowledgeEntry
from symbiote.memory.recall import terms

# How many of a symbiote's newest entries the term ranking looks at.
_TERM_CANDIDATES = 500


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
        """One symbiote's entries related to ``theme``.

        Entries whose name or content contains ``theme`` come first (newest
        first). The remaining slots go to entries sharing terms with it —
        stop words, accents and common suffixes ignored, as in memory recall
        — ranked by the fraction of the theme's terms they contain. So a
        whole user message finds the entry about its subject, not only a
        literal substring of it.
        """
        pattern = like_contains(theme)
        rows = self._storage.fetch_all(
            "SELECT * FROM knowledge_entries "
            f"WHERE symbiote_id = ? AND ({contains('name')} OR {contains('content')}) "
            "ORDER BY created_at DESC LIMIT ?",
            (symbiote_id, pattern, pattern, limit),
        )
        results = [self._row_to_entry(r) for r in rows]
        if len(results) >= limit:
            return results

        query_terms = terms(theme)
        if not query_terms:
            return results
        seen = {e.id for e in results}
        candidates = self._storage.fetch_all(
            "SELECT * FROM knowledge_entries WHERE symbiote_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (symbiote_id, _TERM_CANDIDATES),
        )
        scored: list[tuple[float, int, KnowledgeEntry]] = []
        for position, row in enumerate(candidates):
            if row["id"] in seen:
                continue
            entry = self._row_to_entry(row)
            entry_terms = terms(f"{entry.name}\n{entry.content or ''}")
            for tag in entry.tags:
                entry_terms |= terms(tag)
            shared = len(entry_terms & query_terms)
            if shared:
                # Newer entries win ties (smaller position in the DESC list).
                scored.append((shared / len(query_terms), -position, entry))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        results.extend(entry for _, _, entry in scored[: limit - len(results)])
        return results

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
