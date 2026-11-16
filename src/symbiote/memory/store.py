"""MemoryStore — persist and query memory entries."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from symbiote.core.exceptions import EntityNotFoundError, ValidationError
from symbiote.core.models import MemoryEntry
from symbiote.core.ports import StoragePort
from symbiote.core.sql import contains, like_contains
from symbiote.core.subjects import ANY_SUBJECT, AnySubject, subject_of_session

logger = logging.getLogger(__name__)

# How many of a symbiote's memories (most important / recently used first)
# the term-based recall scores per message.
_RECALL_CANDIDATES = 500


class MemoryStore:
    """Manages persistence and retrieval of MemoryEntry objects.

    Memory is partitioned by symbiote and, inside a symbiote, by end user
    (``subject_key``, see ``symbiote.core.subjects``). Every query method
    takes the ``subject_key`` of the session it is serving and returns the
    shared memories plus that subject's; ``None`` returns the shared ones
    only. ``store`` takes the partition from the entry's session.
    """

    # Read by callers that accept any ``MemoryPort``: this one understands
    # the ``subject_key`` arguments.
    supports_subjects = True

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage
        # ``fn(symbiote_id) -> bool``; set by the kernel from the symbiote's
        # ``memory_enabled``. None means memory is on for everyone.
        self._enabled_check: Callable[[str], bool] | None = None

    def set_enabled_check(self, check: Callable[[str], bool] | None) -> None:
        """Install the per-symbiote switch for long-term memory.

        For a symbiote the check turns off, ``store`` writes nothing and the
        query methods return nothing: this is the single point every writer
        (reflection, failure memory, handoff, dream, consolidation, ``learn``)
        and every reader (context assembly, ``search_memories``) goes through.
        """
        self._enabled_check = check

    def is_enabled(self, symbiote_id: str) -> bool:
        return self._enabled_check is None or self._enabled_check(symbiote_id)

    def subject_for_session(self, session_id: str | None) -> str | None:
        """The end user a session belongs to (None: no end user)."""
        return subject_of_session(self._storage, session_id)

    @staticmethod
    def _partition(
        subject_key: str | None, include_shared: bool = True
    ) -> tuple[str, tuple]:
        """SQL condition (with its parameters) for the entries a caller
        acting for ``subject_key`` reads: the shared ones plus that
        subject's, or only that subject's with ``include_shared=False``.
        Without a subject, the shared ones."""
        if subject_key is None:
            return " AND subject_key IS NULL", ()
        if include_shared:
            return " AND (subject_key IS NULL OR subject_key = ?)", (subject_key,)
        return " AND subject_key = ?", (subject_key,)

    # ── public API ─────────────────────────────────────────────────────

    def store(self, entry: MemoryEntry) -> str:
        """Persist a memory entry and return its ID.

        Writes nothing when long-term memory is off for the entry's symbiote
        (the ID is still returned, so internal writers need no special case).

        The partition comes from the entry's session: an entry written for
        the session of an end user belongs to that end user, whoever the
        writer is (reflection, failure memory, handoff, consolidation,
        ``learn``). ``entry.subject_key`` is filled in accordingly. Raises
        ``ValidationError`` when the entry names a different end user than
        its session's.
        """
        if not self.is_enabled(entry.symbiote_id):
            logger.debug("memory is off for %s; entry not stored", entry.symbiote_id)
            return entry.id
        session_subject = self.subject_for_session(entry.session_id)
        if session_subject is not None:
            if entry.subject_key not in (None, session_subject):
                raise ValidationError(
                    "memory entry names a subject_key other than its session's"
                )
            entry.subject_key = session_subject
        self._storage.execute(
            "INSERT INTO memory_entries "
            "(id, symbiote_id, session_id, subject_key, type, category, scope, "
            "content, tags_json, importance, source, confidence, "
            "created_at, last_used_at, is_active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.id,
                entry.symbiote_id,
                entry.session_id,
                entry.subject_key,
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

    def get(
        self,
        memory_id: str,
        *,
        symbiote_id: str | None = None,
        subject_key: str | None | AnySubject = ANY_SUBJECT,
    ) -> MemoryEntry | None:
        """Fetch a memory entry by ID, returning None if not found.

        With ``symbiote_id``, entries owned by another symbiote are treated as
        not found. With ``subject_key`` (``None`` meaning the shared
        partition), so are entries of any other partition.
        """
        row = self._fetch_row(memory_id, symbiote_id, subject_key)
        if row is None:
            return None
        return self._row_to_entry(row)

    def search(
        self,
        query: str,
        scope: str | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
        *,
        symbiote_id: str,
        subject_key: str | None = None,
    ) -> list[MemoryEntry]:
        """Search one symbiote's active entries by content.

        ``query`` is matched literally (``%`` and ``_`` are not wildcards).
        Optionally filters by scope and tags. Returns the shared entries
        plus the ones of ``subject_key``.
        """
        if not self.is_enabled(symbiote_id):
            return []
        partition, partition_params = self._partition(subject_key)
        sql = (
            "SELECT * FROM memory_entries "
            f"WHERE is_active = 1 AND symbiote_id = ? AND {contains('content')}"
            f"{partition}"
        )
        params: list = [symbiote_id, like_contains(query), *partition_params]

        if scope is not None:
            sql += " AND scope = ?"
            params.append(scope)

        if tags:
            tag_clauses = " OR ".join(contains("tags_json") for _ in tags)
            sql += f" AND ({tag_clauses})"
            for tag in tags:
                params.append(like_contains(f'"{tag}"'))

        sql += " ORDER BY importance DESC, created_at DESC, rowid LIMIT ?"
        params.append(limit)

        rows = self._storage.fetch_all(sql, tuple(params))
        return [self._row_to_entry(r) for r in rows]

    def get_relevant(
        self,
        intent: str,
        session_id: str | None = None,
        limit: int = 5,
        *,
        symbiote_id: str,
        subject_key: str | None = None,
    ) -> list[MemoryEntry]:
        """Return one symbiote's memories that share terms with ``intent``.

        Terms are compared after removing stop words (English and
        Portuguese), accents and common suffixes, so a question finds a
        memory without being a substring of it. Memories from ``session_id``
        come first. Updates last_used_at for all returned entries.

        The candidates are the shared memories plus the ones of
        ``subject_key``. The partition is applied in the query, before the
        candidate limit: other end users' memories never take the place of
        the ones this caller may see.
        """
        if not self.is_enabled(symbiote_id):
            return []
        from symbiote.memory.recall import rank_entries

        partition, partition_params = self._partition(subject_key)
        rows = self._storage.fetch_all(
            "SELECT * FROM memory_entries WHERE is_active = 1 AND symbiote_id = ? "
            # A long-run handoff is a JSON record for the planner (read with
            # get_by_category), not something to show the model as a memory.
            "AND COALESCE(category, '') <> 'handoff'"
            f"{partition} "
            # COALESCE: never-used memories (NULL) sort last on every database.
            "ORDER BY importance DESC, COALESCE(last_used_at, '') DESC, rowid LIMIT ?",
            (symbiote_id, *partition_params, _RECALL_CANDIDATES),
        )
        results = rank_entries(
            [self._row_to_entry(r) for r in rows], intent, session_id, limit
        )
        now = datetime.now(tz=UTC).isoformat()
        with self._storage.transaction():
            for entry in results:
                self._storage.execute(
                    "UPDATE memory_entries SET last_used_at = ? WHERE id = ?",
                    (now, entry.id),
                )
        return results

    def get_standing(
        self,
        symbiote_id: str,
        types: tuple[str, ...],
        limit: int = 5,
        *,
        subject_key: str | None = None,
    ) -> list[MemoryEntry]:
        """Active entries of ``types`` that hold in every turn, the most
        important first: the shared ones plus the ones of ``subject_key``.
        Like a recall, it updates their last_used_at: they are in use, and
        the dream must not prune them as stale."""
        if not self.is_enabled(symbiote_id) or not types or limit <= 0:
            return []
        marks = ", ".join("?" for _ in types)
        partition, partition_params = self._partition(subject_key)
        rows = self._storage.fetch_all(
            "SELECT * FROM memory_entries "
            f"WHERE is_active = 1 AND symbiote_id = ? AND type IN ({marks})"
            f"{partition} "
            "ORDER BY importance DESC, created_at DESC, rowid LIMIT ?",
            (symbiote_id, *types, *partition_params, limit),
        )
        entries = [self._row_to_entry(r) for r in rows]
        now = datetime.now(tz=UTC).isoformat()
        with self._storage.transaction():
            for entry in entries:
                self._storage.execute(
                    "UPDATE memory_entries SET last_used_at = ? WHERE id = ?",
                    (now, entry.id),
                )
        return entries

    def get_by_type(
        self,
        symbiote_id: str,
        entry_type: str,
        limit: int = 20,
        *,
        subject_key: str | None = None,
        include_shared: bool = True,
    ) -> list[MemoryEntry]:
        """Return active entries filtered by symbiote and memory type: the
        shared ones plus the ones of ``subject_key``, or only that subject's
        with ``include_shared=False``."""
        if not self.is_enabled(symbiote_id):
            return []
        partition, partition_params = self._partition(subject_key, include_shared)
        rows = self._storage.fetch_all(
            "SELECT * FROM memory_entries "
            "WHERE is_active = 1 AND symbiote_id = ? AND type = ?"
            f"{partition} "
            "ORDER BY importance DESC, created_at DESC, rowid LIMIT ?",
            (symbiote_id, entry_type, *partition_params, limit),
        )
        return [self._row_to_entry(r) for r in rows]

    def get_by_category(
        self,
        symbiote_id: str,
        category: str,
        limit: int = 20,
        *,
        subject_key: str | None = None,
        include_shared: bool = True,
    ) -> list[MemoryEntry]:
        """Return active entries filtered by symbiote and memory category
        (partitions as in ``get_by_type``)."""
        if not self.is_enabled(symbiote_id):
            return []
        partition, partition_params = self._partition(subject_key, include_shared)
        rows = self._storage.fetch_all(
            "SELECT * FROM memory_entries "
            "WHERE is_active = 1 AND symbiote_id = ? AND category = ?"
            f"{partition} "
            "ORDER BY importance DESC, created_at DESC, rowid LIMIT ?",
            (symbiote_id, category, *partition_params, limit),
        )
        return [self._row_to_entry(r) for r in rows]

    def deactivate(
        self,
        memory_id: str,
        *,
        symbiote_id: str | None = None,
        subject_key: str | None | AnySubject = ANY_SUBJECT,
    ) -> None:
        """Set is_active=False. Raises EntityNotFoundError if not found.

        With ``symbiote_id``, an entry owned by another symbiote is not found.
        With ``subject_key`` (``None`` meaning the shared partition), neither
        is an entry of any other partition: a caller acting for an end user
        passes that user's key and cannot touch anyone else's memory.
        """
        existing = self._fetch_row(memory_id, symbiote_id, subject_key)
        if existing is None:
            raise EntityNotFoundError("MemoryEntry", memory_id)

        self._storage.execute(
            "UPDATE memory_entries SET is_active = 0 WHERE id = ?",
            (memory_id,),
        )

    def update(
        self,
        memory_id: str,
        *,
        content: str | None = None,
        importance: float | None = None,
        tags: list[str] | None = None,
        symbiote_id: str | None = None,
        subject_key: str | None | AnySubject = ANY_SUBJECT,
    ) -> bool:
        """Update a memory entry in place. Returns True if updated, False if id not found.

        With ``symbiote_id``, an entry owned by another symbiote counts as not
        found and is left untouched. With ``subject_key`` (``None`` meaning
        the shared partition), so does an entry of any other partition.

        Only non-None kwargs are written. ``last_used_at`` is intentionally NOT
        touched here — a PATCH from reflection refines the entry, it doesn't
        "use" the memory in the decay sense. ``updated_at`` is bumped so the
        audit / dream pipelines can tell a PATCH from a stale record.

        Inactive entries (``is_active=0``) are not patched and return False —
        prune/reconcile already decided this entry is dead.
        """
        row = self._fetch_row(memory_id, symbiote_id, subject_key)
        if row is None or not row["is_active"]:
            return False

        # Build the UPDATE dynamically so callers can patch any subset.
        sets: list[str] = []
        params: list = []
        if content is not None:
            sets.append("content = ?")
            params.append(content)
        if importance is not None:
            sets.append("importance = ?")
            params.append(float(importance))
        if tags is not None:
            sets.append("tags_json = ?")
            params.append(json.dumps(tags))
        if not sets:
            # No-op patch — caller asked for nothing. Treated as success.
            return True
        sets.append("updated_at = ?")
        params.append(datetime.now(tz=UTC).isoformat())
        params.append(memory_id)
        self._storage.execute(
            f"UPDATE memory_entries SET {', '.join(sets)} WHERE id = ?",
            tuple(params),
        )
        return True

    # ── private helpers ────────────────────────────────────────────────

    def _fetch_row(
        self,
        memory_id: str,
        symbiote_id: str | None,
        subject_key: str | None | AnySubject = ANY_SUBJECT,
    ) -> dict | None:
        if symbiote_id is None:
            row = self._storage.fetch_one(
                "SELECT * FROM memory_entries WHERE id = ?", (memory_id,)
            )
        else:
            row = self._storage.fetch_one(
                "SELECT * FROM memory_entries WHERE id = ? AND symbiote_id = ?",
                (memory_id, symbiote_id),
            )
        if (
            row is not None
            and not isinstance(subject_key, AnySubject)
            and row.get("subject_key") != subject_key
        ):
            return None
        return row

    @staticmethod
    def _row_to_entry(row: dict) -> MemoryEntry:
        created = row["created_at"]
        if isinstance(created, str):
            created = datetime.fromisoformat(created)

        last_used = row["last_used_at"]
        if isinstance(last_used, str):
            last_used = datetime.fromisoformat(last_used)

        updated = row.get("updated_at")
        if isinstance(updated, str) and updated:
            updated = datetime.fromisoformat(updated)
        elif updated is None or updated == "":
            updated = None

        tags = json.loads(row.get("tags_json", "[]"))

        return MemoryEntry(
            id=row["id"],
            symbiote_id=row["symbiote_id"],
            session_id=row.get("session_id"),
            subject_key=row.get("subject_key"),
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
            updated_at=updated,
            is_active=bool(row["is_active"]),
        )
