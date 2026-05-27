"""Tests for MemoryConsolidator — compression-only path.

The Consolidator was deliberately narrowed (Sprint 1 of the LLM-reflection
plan): it now produces a single `session_summary` MemoryEntry per overflow,
and fact extraction (preference/constraint/procedural/etc.) lives solely in
ReflectionEngine on close_session. These tests pin that contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.identity import IdentityManager
from symbiote.core.models import Message
from symbiote.memory.consolidator import MemoryConsolidator
from symbiote.memory.store import MemoryStore
from symbiote.memory.working import WorkingMemory


class MockLLM:
    """Mock LLM that returns a compression summary string."""

    def __init__(self, response: str | None = None) -> None:
        self.calls: list[list[dict]] = []
        self._response = response or (
            "User asked about Python project layout. Decision: use src/ "
            "package structure with pyproject.toml. Files touched: "
            "src/pkg/__init__.py. Open thread: whether to add ruff."
        )

    def complete(self, messages: list[dict], config: dict | None = None) -> str:
        self.calls.append(messages)
        return self._response


class FailingLLM:
    """Mock LLM that raises an exception."""

    def complete(self, messages: list[dict], config: dict | None = None) -> str:
        raise RuntimeError("LLM unavailable")


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "consolidator_test.db"
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


def _fill_working_memory(wm: WorkingMemory, count: int, chars_per_msg: int = 200) -> None:
    """Fill working memory with messages to exceed token threshold."""
    for i in range(count):
        role = "user" if i % 2 == 0 else "assistant"
        content = f"Message {i}: " + "x" * chars_per_msg
        wm.update_message(Message(session_id=wm.session_id, role=role, content=content))


class TestConsolidation:
    def test_no_consolidation_under_threshold(self, store: MemoryStore) -> None:
        llm = MockLLM()
        consolidator = MemoryConsolidator(llm, store, token_threshold=2000)
        wm = WorkingMemory(session_id="s1")
        wm.update_message(Message(session_id="s1", role="user", content="Hi"))

        result = consolidator.consolidate_if_needed(wm, "sym-1")
        assert result == 0
        assert len(llm.calls) == 0

    def test_sync_consolidation_persists_one_summary(
        self, store: MemoryStore, symbiote_id: str
    ) -> None:
        """Sprint 1 contract: one session_summary per overflow, never N facts."""
        llm = MockLLM()
        consolidator = MemoryConsolidator(
            llm, store, token_threshold=100, keep_recent=3
        )
        wm = WorkingMemory(session_id="s1", max_messages=50)
        _fill_working_memory(wm, 12, chars_per_msg=200)

        result = consolidator.consolidate_sync(wm, symbiote_id)
        assert result == 1  # exactly one session_summary
        assert len(llm.calls) == 1

    def test_working_memory_trimmed_immediately(
        self, store: MemoryStore, symbiote_id: str
    ) -> None:
        llm = MockLLM()
        consolidator = MemoryConsolidator(
            llm, store, token_threshold=100, keep_recent=4
        )
        wm = WorkingMemory(session_id="s1", max_messages=50)
        _fill_working_memory(wm, 10, chars_per_msg=200)

        assert len(wm.recent_messages) == 10
        consolidator.consolidate_if_needed(wm, symbiote_id)
        # Working memory trimmed immediately (non-blocking)
        assert len(wm.recent_messages) == 4

    def test_async_consolidation_returns_minus_one(
        self, store: MemoryStore, symbiote_id: str
    ) -> None:
        llm = MockLLM()
        consolidator = MemoryConsolidator(
            llm, store, token_threshold=100, keep_recent=3
        )
        wm = WorkingMemory(session_id="s1", max_messages=50)
        _fill_working_memory(wm, 10, chars_per_msg=200)

        result = consolidator.consolidate_if_needed(wm, symbiote_id)
        assert result == -1  # background task started

    def test_async_starts_background_thread(
        self, store: MemoryStore, symbiote_id: str
    ) -> None:
        llm = MockLLM()
        consolidator = MemoryConsolidator(
            llm, store, token_threshold=100, keep_recent=3
        )
        wm = WorkingMemory(session_id="s1", max_messages=50)
        _fill_working_memory(wm, 10, chars_per_msg=200)

        consolidator.consolidate_if_needed(wm, symbiote_id)

        # Background thread was started
        assert consolidator._last_thread is not None
        assert consolidator._last_thread.daemon is True
        consolidator._last_thread.join(timeout=5.0)
        assert not consolidator._last_thread.is_alive()

    def test_sync_persists_summary_to_store(
        self, store: MemoryStore, symbiote_id: str
    ) -> None:
        """The persisted entry is type=session_summary, source=system."""
        llm = MockLLM()
        consolidator = MemoryConsolidator(
            llm, store, token_threshold=100, keep_recent=3
        )
        wm = WorkingMemory(session_id="s1", max_messages=50)
        _fill_working_memory(wm, 10, chars_per_msg=200)

        consolidator.consolidate_sync(wm, symbiote_id)

        entries = store.search("Python")
        assert len(entries) >= 1
        assert entries[0].source == "system"
        assert entries[0].type == "session_summary"

    def test_fallback_on_llm_failure(
        self, store: MemoryStore, symbiote_id: str
    ) -> None:
        """LLM failure -> naive pipe-joined fallback summary, still 1 entry."""
        llm = FailingLLM()
        consolidator = MemoryConsolidator(
            llm, store, token_threshold=100, keep_recent=3
        )
        wm = WorkingMemory(session_id="s1", max_messages=50)
        _fill_working_memory(wm, 10, chars_per_msg=200)

        result = consolidator.consolidate_sync(wm, symbiote_id)
        # Fallback still produces exactly one session_summary
        assert result == 1
        assert len(wm.recent_messages) == 3

    def test_no_consolidation_with_few_messages(self, store: MemoryStore) -> None:
        """Don't consolidate if messages <= keep_recent even if tokens high."""
        llm = MockLLM()
        consolidator = MemoryConsolidator(
            llm, store, token_threshold=10, keep_recent=6
        )
        wm = WorkingMemory(session_id="s1")
        # 5 long messages but <= keep_recent
        _fill_working_memory(wm, 5, chars_per_msg=500)

        result = consolidator.consolidate_if_needed(wm, "sym-1")
        assert result == 0


class TestCompression:
    """New tests covering the compression-only contract."""

    def test_empty_summary_persists_nothing(
        self, store: MemoryStore, symbiote_id: str
    ) -> None:
        """LLM returning whitespace -> 0 entries persisted."""

        class BlankLLM:
            def complete(self, messages: list[dict], config: dict | None = None) -> str:
                return "   "

        consolidator = MemoryConsolidator(
            BlankLLM(), store, token_threshold=100, keep_recent=3
        )
        wm = WorkingMemory(session_id="s1", max_messages=50)
        _fill_working_memory(wm, 10, chars_per_msg=200)

        result = consolidator.consolidate_sync(wm, symbiote_id)
        assert result == 0

    def test_summary_truncated_to_max_chars(
        self, store: MemoryStore, symbiote_id: str
    ) -> None:
        """Defense against runaway LLM output."""
        huge = "x" * 10_000

        class HugeLLM:
            def complete(self, messages: list[dict], config: dict | None = None) -> str:
                return huge

        consolidator = MemoryConsolidator(
            HugeLLM(), store, token_threshold=100, keep_recent=3
        )
        wm = WorkingMemory(session_id="s1", max_messages=50)
        _fill_working_memory(wm, 10, chars_per_msg=200)

        consolidator.consolidate_sync(wm, symbiote_id)
        entries = store.search("x")
        # _MAX_SUMMARY_CHARS = 2400
        assert all(len(e.content) <= 2400 for e in entries)


class TestChatRunnerIntegration:
    def test_consolidator_called_after_run(self, store: MemoryStore) -> None:
        from symbiote.core.context import AssembledContext
        from symbiote.runners.chat import ChatRunner

        class SimpleLLM:
            def complete(self, messages: list[dict], config: dict | None = None) -> str:
                return "Response"

        consolidation_calls: list[str] = []

        class TrackingConsolidator:
            def consolidate_if_needed(self, wm, symbiote_id):
                consolidation_calls.append(symbiote_id)
                return 0

        wm = WorkingMemory(session_id="s1")
        runner = ChatRunner(
            SimpleLLM(),
            working_memory=wm,
            consolidator=TrackingConsolidator(),
        )
        ctx = AssembledContext(
            symbiote_id="sym-1", session_id="s1", user_input="Hello"
        )
        runner.run(ctx)

        assert consolidation_calls == ["sym-1"]
