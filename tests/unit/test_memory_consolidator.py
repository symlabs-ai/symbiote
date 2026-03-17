"""Tests for MemoryConsolidator — B-10."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.identity import IdentityManager
from symbiote.core.models import Message
from symbiote.memory.consolidator import MemoryConsolidator
from symbiote.memory.store import MemoryStore
from symbiote.memory.working import WorkingMemory


class MockLLM:
    """Mock LLM that returns a consolidation response."""

    def __init__(self, response: str | None = None) -> None:
        self.calls: list[list[dict]] = []
        self._response = response or json.dumps([
            {"content": "User prefers Python", "type": "preference", "importance": 0.7},
            {"content": "Always run tests first", "type": "constraint", "importance": 0.8},
        ])

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

    def test_consolidation_when_over_threshold(
        self, store: MemoryStore, symbiote_id: str
    ) -> None:
        llm = MockLLM()
        consolidator = MemoryConsolidator(
            llm, store, token_threshold=100, keep_recent=3
        )
        wm = WorkingMemory(session_id="s1", max_messages=50)
        _fill_working_memory(wm, 12, chars_per_msg=200)

        result = consolidator.consolidate_if_needed(wm, symbiote_id)
        assert result == 2  # 2 facts from MockLLM
        assert len(llm.calls) == 1  # LLM was called once

    def test_working_memory_trimmed_after_consolidation(
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
        assert len(wm.recent_messages) == 4  # only keep_recent remain

    def test_facts_persisted_to_memory_store(
        self, store: MemoryStore, symbiote_id: str
    ) -> None:
        llm = MockLLM()
        consolidator = MemoryConsolidator(
            llm, store, token_threshold=100, keep_recent=3
        )
        wm = WorkingMemory(session_id="s1", max_messages=50)
        _fill_working_memory(wm, 10, chars_per_msg=200)

        consolidator.consolidate_if_needed(wm, symbiote_id)

        # Check entries in memory store
        entries = store.search("Python")
        assert len(entries) >= 1
        assert entries[0].source == "system"
        assert entries[0].scope == "session"

    def test_fallback_on_llm_failure(
        self, store: MemoryStore, symbiote_id: str
    ) -> None:
        llm = FailingLLM()
        consolidator = MemoryConsolidator(
            llm, store, token_threshold=100, keep_recent=3
        )
        wm = WorkingMemory(session_id="s1", max_messages=50)
        _fill_working_memory(wm, 10, chars_per_msg=200)

        result = consolidator.consolidate_if_needed(wm, symbiote_id)
        # Fallback creates a summary fact
        assert result >= 1
        # Working memory still trimmed
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


class TestParseFacts:
    def test_parses_valid_json_array(self) -> None:
        response = json.dumps([
            {"content": "fact1", "type": "factual", "importance": 0.5},
        ])
        facts = MemoryConsolidator._parse_facts(response)
        assert len(facts) == 1
        assert facts[0]["content"] == "fact1"

    def test_parses_json_in_code_block(self) -> None:
        response = '```json\n[{"content": "fact", "type": "factual", "importance": 0.5}]\n```'
        facts = MemoryConsolidator._parse_facts(response)
        assert len(facts) == 1

    def test_returns_empty_on_invalid_json(self) -> None:
        assert MemoryConsolidator._parse_facts("not json") == []

    def test_filters_entries_without_content(self) -> None:
        response = json.dumps([
            {"content": "valid", "type": "factual"},
            {"type": "factual"},  # no content
        ])
        facts = MemoryConsolidator._parse_facts(response)
        assert len(facts) == 1


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
