"""MemoryStore — persist and query memory entries."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from symbiote.core.exceptions import EntityNotFoundError
from symbiote.core.models import MemoryEntry
from symbiote.core.ports import StoragePort


class MemoryStore:
    """Manages persistence and retrieval of MemoryEntry objects."""

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage

    # ── public API ─────────────────────────────────────────────────────

    def store(self, entry: MemoryEntry) -> str:
        """Persist a memory entry and return its ID."""
        self._storage.execute(
            "INSERT INTO memory_entries "
            "(id, symbiote_id, session_id, type, category, scope, content, "
            "tags_json, importance, source, confidence, "
            "created_at, last_used_at, is_active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.id,
                entry.symbiote_id,
                entry.session_id,
                entry.type,
                entry.category,
                entry.scope,
                entry.content,
                json.dumps(entry.tags),
                entry.importance,
                entry.source,
                entry.confidence,
                entry.created_at.isoformat(),
                entry.last_used_at.isoformat(),
                1 if entry.is_active else 0,
            ),
        )
        return entry.id

    def get(self, memory_id: str) -> MemoryEntry | None:
        """Fetch a memory entry by ID, returning None if not found."""
        row = self._storage.fetch_one(
            "SELECT * FROM memory_entries WHERE id = ?", (memory_id,)
        )
        if row is None:
            return None
        return self._row_to_entry(row)

    def search(
        self,
        query: str,
        scope: str | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
    ) -> list[MemoryEntry]:
        """Search active entries by content, optionally filtering by scope and tags."""
        sql = (
            "SELECT * FROM memory_entries "
            "WHERE is_active = 1 AND content LIKE ?"
        )
        params: list = [f"%{query}%"]

        if scope is not None:
            sql += " AND scope = ?"
            params.append(scope)

        if tags:
            tag_clauses = " OR ".join(
                "tags_json LIKE ?" for _ in tags
            )
            sql += f" AND ({tag_clauses})"
            for tag in tags:
                params.append(f'%"{tag}"%')

        sql += " ORDER BY importance DESC, created_at DESC LIMIT ?"
        params.append(limit)

        rows = self._storage.fetch_all(sql, tuple(params))
        return [self._row_to_entry(r) for r in rows]

    def get_relevant(
        self,
        intent: str,
        session_id: str | None = None,
        limit: int = 5,
    ) -> list[MemoryEntry]:
        """Return relevant memories, prioritising session proximity then content match.

        Updates last_used_at for all returned entries.
        """
        results: list[MemoryEntry] = []
        seen_ids: set[str] = set()

        # 1) Session-specific matches first
        if session_id is not None:
            session_rows = self._storage.fetch_all(
                "SELECT * FROM memory_entries "
                "WHERE is_active = 1 AND session_id = ? AND content LIKE ? "
                "ORDER BY importance DESC, last_used_at DESC LIMIT ?",
                (session_id, f"%{intent}%", limit),
            )
            for row in session_rows:
                entry = self._row_to_entry(row)
                results.append(entry)
                seen_ids.add(entry.id)

        # 2) Content matches (excluding already seen)
        remaining = limit - len(results)
        if remaining > 0:
            content_rows = self._storage.fetch_all(
                "SELECT * FROM memory_entries "
                "WHERE is_active = 1 AND content LIKE ? "
                "ORDER BY importance DESC, last_used_at DESC LIMIT ?",
                (f"%{intent}%", limit),
            )
            for row in content_rows:
                if row["id"] not in seen_ids:
                    results.append(self._row_to_entry(row))
                    seen_ids.add(row["id"])
                    if len(results) >= limit:
                        break

        # Update last_used_at for returned entries
        now = datetime.now(tz=UTC).isoformat()
        for entry in results:
            self._storage.execute(
                "UPDATE memory_entries SET last_used_at = ? WHERE id = ?",
                (now, entry.id),
            )

        return results

    def get_by_type(
        self, symbiote_id: str, entry_type: str, limit: int = 20
    ) -> list[MemoryEntry]:
        """Return active entries filtered by symbiote and memory type."""
        rows = self._storage.fetch_all(
            "SELECT * FROM memory_entries "
            "WHERE is_active = 1 AND symbiote_id = ? AND type = ? "
            "ORDER BY importance DESC, created_at DESC LIMIT ?",
            (symbiote_id, entry_type, limit),
        )
        return [self._row_to_entry(r) for r in rows]

    def get_by_category(
        self, symbiote_id: str, category: str, limit: int = 20
    ) -> list[MemoryEntry]:
        """Return active entries filtered by symbiote and memory category."""
        rows = self._storage.fetch_all(
            "SELECT * FROM memory_entries "
            "WHERE is_active = 1 AND symbiote_id = ? AND category = ? "
            "ORDER BY importance DESC, created_at DESC LIMIT ?",
            (symbiote_id, category, limit),
        )
        return [self._row_to_entry(r) for r in rows]

    def deactivate(self, memory_id: str) -> None:
        """Set is_active=False. Raises EntityNotFoundError if not found."""
        existing = self._storage.fetch_one(
            "SELECT id FROM memory_entries WHERE id = ?", (memory_id,)
        )
        if existing is None:
            raise EntityNotFoundError("MemoryEntry", memory_id)

        self._storage.execute(
            "UPDATE memory_entries SET is_active = 0 WHERE id = ?",
            (memory_id,),
        )

    # ── private helpers ────────────────────────────────────────────────

    @staticmethod
    def _row_to_entry(row: dict) -> MemoryEntry:
        created = row["created_at"]
        if isinstance(created, str):
            created = datetime.fromisoformat(created)

        last_used = row["last_used_at"]
        if isinstance(last_used, str):
            last_used = datetime.fromisoformat(last_used)

        tags = json.loads(row.get("tags_json", "[]"))

        return MemoryEntry(
            id=row["id"],
            symbiote_id=row["symbiote_id"],
            session_id=row.get("session_id"),
            type=row["type"],
            category=row.get("category"),
            scope=row["scope"],
            content=row["content"],
            tags=tags,
            importance=row["importance"],
            source=row["source"],
            confidence=row["confidence"],
            created_at=created,
            last_used_at=last_used,
            is_active=bool(row["is_active"]),
        )
