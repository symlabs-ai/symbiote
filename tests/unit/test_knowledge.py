"""Tests for KnowledgeService — T-09."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.exceptions import EntityNotFoundError
from symbiote.core.identity import IdentityManager
from symbiote.knowledge.models import KnowledgeEntry
from symbiote.knowledge.service import KnowledgeService


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "knowledge_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    """Create a symbiote row and return its ID."""
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name="TestBot", role="assistant")
    return sym.id


@pytest.fixture()
def service(adapter: SQLiteAdapter) -> KnowledgeService:
    return KnowledgeService(storage=adapter)


# ── Register Source ────────────────────────────────────────────────────────


class TestRegisterSource:
    def test_register_persisted_and_retrievable(
        self, service: KnowledgeService, symbiote_id: str
    ) -> None:
        entry = service.register_source(
            symbiote_id=symbiote_id,
            name="Design Doc",
        )
        assert isinstance(entry, KnowledgeEntry)
        assert entry.name == "Design Doc"
        assert entry.symbiote_id == symbiote_id
        assert entry.type == "document"
        assert entry.tags == []

        fetched = service.get(entry.id)
        assert fetched is not None
        assert fetched.id == entry.id
        assert fetched.name == "Design Doc"

    def test_register_with_all_fields(
        self, service: KnowledgeService, symbiote_id: str
    ) -> None:
        entry = service.register_source(
            symbiote_id=symbiote_id,
            name="API Reference",
            source_path="/docs/api.md",
            content="Full API documentation for the system.",
            entry_type="reference",
            tags=["api", "docs"],
        )
        assert entry.name == "API Reference"
        assert entry.source_path == "/docs/api.md"
        assert entry.content == "Full API documentation for the system."
        assert entry.type == "reference"
        assert entry.tags == ["api", "docs"]

        fetched = service.get(entry.id)
        assert fetched is not None
        assert fetched.source_path == "/docs/api.md"
        assert fetched.content == "Full API documentation for the system."
        assert fetched.type == "reference"
        assert fetched.tags == ["api", "docs"]


# ── Get ────────────────────────────────────────────────────────────────────


class TestGet:
    def test_get_nonexistent_returns_none(
        self, service: KnowledgeService
    ) -> None:
        assert service.get("does-not-exist") is None


# ── Query ──────────────────────────────────────────────────────────────────


class TestQuery:
    def test_query_matches_content_and_name(
        self, service: KnowledgeService, symbiote_id: str
    ) -> None:
        service.register_source(
            symbiote_id=symbiote_id,
            name="Architecture Overview",
            content="Describes the layered architecture.",
        )
        service.register_source(
            symbiote_id=symbiote_id,
            name="Deployment Guide",
            content="Steps to deploy the application.",
        )

        # Match by name
        results = service.query(symbiote_id=symbiote_id, theme="Architecture")
        assert len(results) == 1
        assert results[0].name == "Architecture Overview"

        # Match by content
        results = service.query(symbiote_id=symbiote_id, theme="deploy")
        assert len(results) == 1
        assert results[0].name == "Deployment Guide"

    def test_query_filters_by_symbiote_id(
        self, service: KnowledgeService, adapter: SQLiteAdapter
    ) -> None:
        mgr = IdentityManager(storage=adapter)
        sym_a = mgr.create(name="BotA", role="assistant")
        sym_b = mgr.create(name="BotB", role="assistant")

        service.register_source(
            symbiote_id=sym_a.id,
            name="Shared Topic",
            content="Important info about shared topic.",
        )
        service.register_source(
            symbiote_id=sym_b.id,
            name="Shared Topic",
            content="Different info about shared topic.",
        )

        results_a = service.query(symbiote_id=sym_a.id, theme="Shared")
        assert len(results_a) == 1
        assert results_a[0].symbiote_id == sym_a.id

        results_b = service.query(symbiote_id=sym_b.id, theme="Shared")
        assert len(results_b) == 1
        assert results_b[0].symbiote_id == sym_b.id

    def test_query_respects_limit(
        self, service: KnowledgeService, symbiote_id: str
    ) -> None:
        for i in range(5):
            service.register_source(
                symbiote_id=symbiote_id,
                name=f"Doc {i}",
                content="Common keyword searchable.",
            )

        results = service.query(
            symbiote_id=symbiote_id, theme="searchable", limit=3
        )
        assert len(results) == 3


# ── List by Symbiote ──────────────────────────────────────────────────────


class TestListBySymbiote:
    def test_list_returns_correct_entries(
        self, service: KnowledgeService, symbiote_id: str
    ) -> None:
        service.register_source(
            symbiote_id=symbiote_id, name="Entry A"
        )
        service.register_source(
            symbiote_id=symbiote_id, name="Entry B"
        )

        results = service.list_by_symbiote(symbiote_id)
        assert len(results) == 2
        names = {e.name for e in results}
        assert names == {"Entry A", "Entry B"}

    def test_list_empty(self, service: KnowledgeService) -> None:
        assert service.list_by_symbiote("no-such-symbiote") == []


# ── Remove ─────────────────────────────────────────────────────────────────


class TestRemove:
    def test_remove_deletes_entry(
        self, service: KnowledgeService, symbiote_id: str
    ) -> None:
        entry = service.register_source(
            symbiote_id=symbiote_id, name="Ephemeral"
        )
        service.remove(entry.id)
        assert service.get(entry.id) is None

    def test_remove_nonexistent_raises(
        self, service: KnowledgeService
    ) -> None:
        with pytest.raises(EntityNotFoundError, match="not found"):
            service.remove("ghost-id")
