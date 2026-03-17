"""Tests for SemanticRecallProvider — B-4."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.identity import IdentityManager
from symbiote.core.models import MemoryEntry
from symbiote.memory.recall import (
    SemanticRecallProvider,
    score_entry,
    tokenize,
)
from symbiote.memory.store import MemoryStore


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "recall_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    return IdentityManager(storage=adapter).create(name="Bot", role="assistant").id


@pytest.fixture()
def store(adapter: SQLiteAdapter) -> MemoryStore:
    return MemoryStore(storage=adapter)


@pytest.fixture()
def provider(adapter: SQLiteAdapter) -> SemanticRecallProvider:
    return SemanticRecallProvider(adapter)


def _add_memory(
    store: MemoryStore, symbiote_id: str, content: str, *,
    importance: float = 0.5, tags: list[str] | None = None,
    session_id: str | None = None,
) -> MemoryEntry:
    entry = MemoryEntry(
        symbiote_id=symbiote_id,
        session_id=session_id,
        type="factual",
        scope="global",
        content=content,
        importance=importance,
        source="user",
        tags=tags or [],
    )
    store.store(entry)
    return entry


# ── tokenize ─────────────────────────────────────────────────────────────


class TestTokenize:
    def test_basic_tokenization(self) -> None:
        tokens = tokenize("I prefer Python over JavaScript")
        assert "python" in tokens
        assert "javascript" in tokens
        assert "prefer" in tokens
        # stop words filtered
        assert "i" not in tokens

    def test_filters_stop_words(self) -> None:
        tokens = tokenize("the quick brown fox is very fast")
        assert "quick" in tokens
        assert "brown" in tokens
        assert "fox" in tokens
        assert "fast" in tokens
        assert "the" not in tokens
        assert "is" not in tokens
        assert "very" not in tokens

    def test_single_char_filtered(self) -> None:
        tokens = tokenize("a b c real words")
        assert "a" not in tokens
        assert "b" not in tokens
        assert "real" in tokens
        assert "words" in tokens

    def test_empty_string(self) -> None:
        assert tokenize("") == set()

    def test_handles_punctuation(self) -> None:
        tokens = tokenize("Hello, world! Testing (brackets)")
        assert "hello" in tokens
        assert "world" in tokens


# ── score_entry ──────────────────────────────────────────────────────────


class TestScoreEntry:
    def test_perfect_overlap(self) -> None:
        score = score_entry({"python", "code"}, {"python", "code"}, 0.5, 1.0)
        assert score == pytest.approx(0.5 + 0.15 + 0.2)  # 0.85

    def test_partial_overlap(self) -> None:
        score = score_entry({"python"}, {"python", "code"}, 0.5, 1.0)
        assert score < 0.85  # less than perfect

    def test_no_overlap(self) -> None:
        score = score_entry({"java"}, {"python", "code"}, 0.5, 1.0)
        assert score == pytest.approx(0.0 + 0.15 + 0.2)  # 0.35

    def test_high_importance_boosts_score(self) -> None:
        low = score_entry({"python"}, {"python"}, 0.3, 0.5)
        high = score_entry({"python"}, {"python"}, 0.9, 0.5)
        assert high > low

    def test_empty_query(self) -> None:
        score = score_entry({"python"}, set(), 0.7, 1.0)
        assert score == 0.7  # falls back to importance


# ── SemanticRecallProvider ───────────────────────────────────────────────


class TestSemanticRecall:
    def test_recalls_relevant_entries(
        self, provider: SemanticRecallProvider, store: MemoryStore, symbiote_id: str
    ) -> None:
        _add_memory(store, symbiote_id, "User prefers Python for backend development")
        _add_memory(store, symbiote_id, "Project uses PostgreSQL database")
        _add_memory(store, symbiote_id, "Weather is sunny today")

        results = provider.recall("What language for the backend?")
        assert len(results) >= 1
        assert any("Python" in e.content for e in results)

    def test_empty_query_returns_nothing(
        self, provider: SemanticRecallProvider
    ) -> None:
        results = provider.recall("")
        assert results == []

    def test_stop_words_only_returns_nothing(
        self, provider: SemanticRecallProvider
    ) -> None:
        results = provider.recall("the is and or")
        assert results == []

    def test_respects_limit(
        self, provider: SemanticRecallProvider, store: MemoryStore, symbiote_id: str
    ) -> None:
        for i in range(10):
            _add_memory(store, symbiote_id, f"Python fact number {i}")

        results = provider.recall("Python facts", limit=3)
        assert len(results) <= 3

    def test_importance_affects_ranking(
        self, provider: SemanticRecallProvider, store: MemoryStore, symbiote_id: str
    ) -> None:
        _add_memory(store, symbiote_id, "Python is great for scripting", importance=0.3)
        _add_memory(store, symbiote_id, "Python is essential for our backend", importance=0.9)

        results = provider.recall("Python backend", limit=2)
        assert len(results) == 2
        # Higher importance should rank first
        assert results[0].importance >= results[1].importance

    def test_tags_contribute_to_matching(
        self, provider: SemanticRecallProvider, store: MemoryStore, symbiote_id: str
    ) -> None:
        _add_memory(
            store, symbiote_id,
            "Use strict mode",
            tags=["typescript", "frontend"],
        )
        _add_memory(store, symbiote_id, "Unrelated memory about cooking")

        results = provider.recall("typescript configuration")
        assert len(results) >= 1
        assert "strict" in results[0].content.lower()

    def test_no_matching_entries(
        self, provider: SemanticRecallProvider, store: MemoryStore, symbiote_id: str
    ) -> None:
        _add_memory(store, symbiote_id, "The weather is nice")
        results = provider.recall("quantum computing algorithms")
        assert results == []

    def test_updates_last_used_at(
        self, provider: SemanticRecallProvider, store: MemoryStore, symbiote_id: str
    ) -> None:
        entry = _add_memory(store, symbiote_id, "Python coding standards")
        original_used = entry.last_used_at

        results = provider.recall("Python standards")
        assert len(results) >= 1

        # Re-fetch from store
        updated = store.get(results[0].id)
        assert updated.last_used_at >= original_used
