"""Tests for MemoryStore — T-08."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.exceptions import EntityNotFoundError
from symbiote.core.identity import IdentityManager
from symbiote.core.models import MemoryEntry
from symbiote.memory.store import MemoryStore


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "memory_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name="MemBot", role="assistant")
    return sym.id


@pytest.fixture()
def session_id(adapter: SQLiteAdapter, symbiote_id: str) -> str:
    sid = str(uuid4())
    adapter.execute(
        "INSERT INTO sessions (id, symbiote_id, status, started_at) "
        "VALUES (?, ?, 'active', datetime('now'))",
        (sid, symbiote_id),
    )
    return sid


@pytest.fixture()
def store(adapter: SQLiteAdapter) -> MemoryStore:
    return MemoryStore(storage=adapter)


def _make_entry(
    symbiote_id: str,
    content: str = "some memory",
    *,
    session_id: str | None = None,
    scope: str = "global",
    tags: list[str] | None = None,
    importance: float = 0.5,
    type: str = "factual",
    source: str = "user",
    is_active: bool = True,
) -> MemoryEntry:
    return MemoryEntry(
        symbiote_id=symbiote_id,
        session_id=session_id,
        type=type,
        scope=scope,
        content=content,
        tags=tags or [],
        importance=importance,
        source=source,
        is_active=is_active,
    )


# ── Store & Get ────────────────────────────────────────────────────────────


class TestStoreAndGet:
    def test_store_persisted_and_retrievable(
        self, store: MemoryStore, symbiote_id: str
    ) -> None:
        entry = _make_entry(symbiote_id, "User prefers dark mode")
        returned_id = store.store(entry)
        assert returned_id == entry.id

        fetched = store.get(entry.id)
        assert fetched is not None
        assert fetched.id == entry.id
        assert fetched.content == "User prefers dark mode"
        assert fetched.symbiote_id == symbiote_id
        assert fetched.is_active is True

    def test_store_serializes_tags(
        self, store: MemoryStore, symbiote_id: str
    ) -> None:
        entry = _make_entry(symbiote_id, "tagged memory", tags=["python", "ai"])
        store.store(entry)

        fetched = store.get(entry.id)
        assert fetched is not None
        assert fetched.tags == ["python", "ai"]

    def test_get_nonexistent_returns_none(self, store: MemoryStore) -> None:
        assert store.get("nonexistent-id") is None


# ── Search ─────────────────────────────────────────────────────────────────


class TestSearch:
    def test_search_by_content(
        self, store: MemoryStore, symbiote_id: str
    ) -> None:
        store.store(_make_entry(symbiote_id, "Python is great"))
        store.store(_make_entry(symbiote_id, "Rust is fast"))

        results = store.search("Python")
        assert len(results) == 1
        assert results[0].content == "Python is great"

    def test_search_by_scope(
        self, store: MemoryStore, symbiote_id: str
    ) -> None:
        store.store(_make_entry(symbiote_id, "global fact", scope="global"))
        store.store(_make_entry(symbiote_id, "project fact", scope="project"))

        results = store.search("fact", scope="project")
        assert len(results) == 1
        assert results[0].scope == "project"

    def test_search_by_tags_any_match(
        self, store: MemoryStore, symbiote_id: str
    ) -> None:
        store.store(
            _make_entry(symbiote_id, "entry A", tags=["python", "web"])
        )
        store.store(
            _make_entry(symbiote_id, "entry B", tags=["rust", "cli"])
        )
        store.store(
            _make_entry(symbiote_id, "entry C", tags=["python", "ml"])
        )

        results = store.search("entry", tags=["rust"])
        assert len(results) == 1
        assert results[0].content == "entry B"

    def test_search_respects_limit(
        self, store: MemoryStore, symbiote_id: str
    ) -> None:
        for i in range(5):
            store.store(_make_entry(symbiote_id, f"item {i}"))

        results = store.search("item", limit=3)
        assert len(results) == 3

    def test_search_only_returns_active(
        self, store: MemoryStore, symbiote_id: str
    ) -> None:
        entry = _make_entry(symbiote_id, "active memory")
        store.store(entry)
        store.store(_make_entry(symbiote_id, "another active"))

        # Deactivate the first entry
        store.deactivate(entry.id)

        results = store.search("memory")
        assert len(results) == 0

    def test_search_ordered_by_importance_desc(
        self, store: MemoryStore, symbiote_id: str
    ) -> None:
        store.store(_make_entry(symbiote_id, "low imp", importance=0.2))
        store.store(_make_entry(symbiote_id, "high imp", importance=0.9))
        store.store(_make_entry(symbiote_id, "mid imp", importance=0.5))

        results = store.search("imp")
        assert results[0].content == "high imp"
        assert results[1].content == "mid imp"
        assert results[2].content == "low imp"


# ── Get Relevant ───────────────────────────────────────────────────────────


class TestGetRelevant:
    def test_session_proximity_then_content(
        self, store: MemoryStore, symbiote_id: str, session_id: str
    ) -> None:
        # Entry matching session
        store.store(
            _make_entry(
                symbiote_id,
                "session specific note",
                session_id=session_id,
                importance=0.3,
            )
        )
        # Entry matching content but different session
        store.store(
            _make_entry(
                symbiote_id,
                "relevant note about intent",
                importance=0.9,
            )
        )

        results = store.get_relevant(
            "note", session_id=session_id, limit=5
        )
        # Session match should come first even with lower importance
        assert len(results) == 2
        assert results[0].session_id == session_id

    def test_get_relevant_updates_last_used_at(
        self, store: MemoryStore, symbiote_id: str
    ) -> None:
        entry = _make_entry(symbiote_id, "memory to recall")
        store.store(entry)
        original_used_at = entry.last_used_at

        results = store.get_relevant("recall")
        assert len(results) == 1

        fetched = store.get(entry.id)
        assert fetched is not None
        assert fetched.last_used_at >= original_used_at

    def test_get_relevant_only_active(
        self, store: MemoryStore, symbiote_id: str
    ) -> None:
        entry = _make_entry(symbiote_id, "soon inactive")
        store.store(entry)
        store.deactivate(entry.id)

        results = store.get_relevant("inactive")
        assert len(results) == 0


# ── Get by Type ────────────────────────────────────────────────────────────


class TestGetByType:
    def test_filters_by_symbiote_and_type(
        self, store: MemoryStore, symbiote_id: str
    ) -> None:
        store.store(
            _make_entry(symbiote_id, "a preference", type="preference")
        )
        store.store(
            _make_entry(symbiote_id, "a fact", type="factual")
        )
        store.store(
            _make_entry(symbiote_id, "another pref", type="preference")
        )

        results = store.get_by_type(symbiote_id, "preference")
        assert len(results) == 2
        assert all(r.type == "preference" for r in results)

    def test_get_by_type_only_active(
        self, store: MemoryStore, symbiote_id: str
    ) -> None:
        entry = _make_entry(symbiote_id, "will deactivate", type="factual")
        store.store(entry)
        store.deactivate(entry.id)

        results = store.get_by_type(symbiote_id, "factual")
        assert len(results) == 0


# ── Deactivate ─────────────────────────────────────────────────────────────


class TestDeactivate:
    def test_deactivate_sets_inactive(
        self, store: MemoryStore, symbiote_id: str
    ) -> None:
        entry = _make_entry(symbiote_id, "to deactivate")
        store.store(entry)

        store.deactivate(entry.id)
        fetched = store.get(entry.id)
        assert fetched is not None
        assert fetched.is_active is False

    def test_deactivate_nonexistent_raises(self, store: MemoryStore) -> None:
        with pytest.raises(EntityNotFoundError, match="not found"):
            store.deactivate("ghost-memory-id")
