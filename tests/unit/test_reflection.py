"""Tests for ReflectionEngine — T-18."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from symbiote.adapters.storage.message_repository import MessageRepository
from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.identity import IdentityManager
from symbiote.core.models import MemoryEntry
from symbiote.core.reflection import ReflectionEngine, ReflectionResult
from symbiote.memory.store import MemoryStore


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "reflection_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name="ReflectBot", role="assistant")
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


@pytest.fixture()
def engine(store: MemoryStore, adapter: SQLiteAdapter) -> ReflectionEngine:
    return ReflectionEngine(memory_store=store, messages=MessageRepository(adapter))


def _insert_message(
    adapter: SQLiteAdapter, session_id: str, role: str, content: str
) -> None:
    adapter.execute(
        "INSERT INTO messages (id, session_id, role, content, created_at) "
        "VALUES (?, ?, ?, ?, datetime('now'))",
        (str(uuid4()), session_id, role, content),
    )


# ── ReflectionResult model ────────────────────────────────────────────────


class TestReflectionResult:
    def test_defaults(self) -> None:
        result = ReflectionResult(session_id="s1", summary="test")
        assert result.extracted_facts == []
        assert result.discarded_count == 0
        assert result.persisted_count == 0


# ── _is_noise ─────────────────────────────────────────────────────────────


class TestIsNoise:
    def test_short_messages_are_noise(self, engine: ReflectionEngine) -> None:
        assert engine._is_noise("ok") is True
        assert engine._is_noise("yes") is True
        assert engine._is_noise("no") is True

    def test_common_patterns_are_noise(self, engine: ReflectionEngine) -> None:
        assert engine._is_noise("thanks") is True
        assert engine._is_noise("got it") is True
        assert engine._is_noise("Thanks!") is True
        assert engine._is_noise("OK") is True

    def test_real_content_not_noise(self, engine: ReflectionEngine) -> None:
        assert engine._is_noise("I prefer using dark mode for all editors") is False
        assert engine._is_noise("Always run tests before committing code") is False


# ── _extract_durable_facts ────────────────────────────────────────────────


class TestExtractDurableFacts:
    def test_finds_preference_keywords(self, engine: ReflectionEngine) -> None:
        messages = [
            {"role": "user", "content": "I prefer Python over JavaScript"},
            {"role": "assistant", "content": "Noted, I'll use Python."},
        ]
        facts = engine._extract_durable_facts(messages)
        assert len(facts) >= 1
        assert any("prefer" in f["content"].lower() for f in facts)

    def test_finds_constraint_keywords(self, engine: ReflectionEngine) -> None:
        messages = [
            {"role": "user", "content": "Never use global variables in this project"},
            {"role": "user", "content": "Always add type hints to functions"},
        ]
        facts = engine._extract_durable_facts(messages)
        assert len(facts) >= 2

    def test_finds_procedure_keywords(self, engine: ReflectionEngine) -> None:
        messages = [
            {"role": "user", "content": "The procedure is to run lint before commit"},
        ]
        facts = engine._extract_durable_facts(messages)
        assert len(facts) >= 1

    def test_no_facts_from_generic_messages(self, engine: ReflectionEngine) -> None:
        messages = [
            {"role": "user", "content": "Hello, how are you?"},
            {"role": "assistant", "content": "I'm doing well, thanks!"},
        ]
        facts = engine._extract_durable_facts(messages)
        assert len(facts) == 0

    def test_fact_structure(self, engine: ReflectionEngine) -> None:
        messages = [
            {"role": "user", "content": "I prefer tabs over spaces"},
        ]
        facts = engine._extract_durable_facts(messages)
        assert len(facts) == 1
        fact = facts[0]
        assert "content" in fact
        assert "type" in fact
        assert "importance" in fact


# ── _generate_summary ─────────────────────────────────────────────────────


class TestGenerateSummary:
    def test_excludes_noise(self, engine: ReflectionEngine) -> None:
        messages = [
            {"role": "user", "content": "Set up the dev environment with Docker"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "Also add a Makefile for common tasks"},
        ]
        summary = engine._generate_summary(messages)
        assert "Docker" in summary
        assert "Makefile" in summary
        assert "ok" not in summary.split()  # 'ok' alone should be excluded

    def test_empty_messages(self, engine: ReflectionEngine) -> None:
        summary = engine._generate_summary([])
        assert summary == ""

    def test_all_noise(self, engine: ReflectionEngine) -> None:
        messages = [
            {"role": "user", "content": "ok"},
            {"role": "assistant", "content": "thanks"},
        ]
        summary = engine._generate_summary(messages)
        assert summary == ""


# ── reflect_session ───────────────────────────────────────────────────────


class TestReflectSession:
    def test_extracts_facts_from_messages(
        self,
        engine: ReflectionEngine,
        adapter: SQLiteAdapter,
        session_id: str,
        symbiote_id: str,
    ) -> None:
        _insert_message(adapter, session_id, "user", "I prefer dark mode in all editors")
        _insert_message(adapter, session_id, "assistant", "I'll remember that preference.")
        _insert_message(adapter, session_id, "user", "Never commit .env files to git")

        result = engine.reflect_session(session_id, symbiote_id)

        assert isinstance(result, ReflectionResult)
        assert result.session_id == session_id
        assert len(result.extracted_facts) >= 2
        assert result.persisted_count >= 2
        assert result.summary != ""

    def test_noise_only_messages(
        self,
        engine: ReflectionEngine,
        adapter: SQLiteAdapter,
        session_id: str,
        symbiote_id: str,
    ) -> None:
        _insert_message(adapter, session_id, "user", "ok")
        _insert_message(adapter, session_id, "assistant", "thanks")
        _insert_message(adapter, session_id, "user", "yes")

        result = engine.reflect_session(session_id, symbiote_id)

        assert result.extracted_facts == []
        assert result.discarded_count == 3
        assert result.persisted_count == 0

    def test_persists_facts_to_memory_store(
        self,
        engine: ReflectionEngine,
        store: MemoryStore,
        adapter: SQLiteAdapter,
        session_id: str,
        symbiote_id: str,
    ) -> None:
        _insert_message(
            adapter, session_id, "user",
            "The rule is to always write tests before implementation"
        )

        result = engine.reflect_session(session_id, symbiote_id)

        assert result.persisted_count >= 1

        # Verify entries exist in memory store
        entries = store.get_by_type(symbiote_id, "constraint")
        assert len(entries) >= 1
        assert entries[0].source == "reflection"
        assert entries[0].session_id == session_id

    def test_mixed_messages(
        self,
        engine: ReflectionEngine,
        adapter: SQLiteAdapter,
        session_id: str,
        symbiote_id: str,
    ) -> None:
        _insert_message(adapter, session_id, "user", "ok")
        _insert_message(adapter, session_id, "user", "I prefer using pytest over unittest")
        _insert_message(adapter, session_id, "assistant", "got it")
        _insert_message(adapter, session_id, "user", "The convention is snake_case for all Python files")

        result = engine.reflect_session(session_id, symbiote_id)

        assert result.discarded_count == 2  # "ok" and "got it"
        assert len(result.extracted_facts) >= 2
        assert result.summary != ""


# ── reflect_task ──────────────────────────────────────────────────────────


class TestReflectTask:
    def test_generates_result_with_task_context(
        self,
        engine: ReflectionEngine,
        adapter: SQLiteAdapter,
        session_id: str,
        symbiote_id: str,
    ) -> None:
        _insert_message(
            adapter, session_id, "user",
            "Always validate input before processing"
        )

        result = engine.reflect_task(
            session_id, symbiote_id,
            task_description="Implement input validation layer"
        )

        assert isinstance(result, ReflectionResult)
        assert result.session_id == session_id

    def test_persists_valuable_learnings(
        self,
        engine: ReflectionEngine,
        store: MemoryStore,
        adapter: SQLiteAdapter,
        session_id: str,
        symbiote_id: str,
    ) -> None:
        _insert_message(
            adapter, session_id, "user",
            "The constraint is: no external API calls in unit tests"
        )

        result = engine.reflect_task(
            session_id, symbiote_id,
            task_description="Set up testing infrastructure"
        )

        assert result.persisted_count >= 1
        entries = store.get_by_type(symbiote_id, "constraint")
        assert len(entries) >= 1

    def test_empty_session_returns_result(
        self,
        engine: ReflectionEngine,
        session_id: str,
        symbiote_id: str,
    ) -> None:
        result = engine.reflect_task(
            session_id, symbiote_id,
            task_description="Some task"
        )

        assert isinstance(result, ReflectionResult)
        assert result.extracted_facts == []
        assert result.persisted_count == 0
