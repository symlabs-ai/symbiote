"""Tests for Hermes-inspired adaptations (B-52, B-53, B-54)."""

from __future__ import annotations

import pytest

from symbiote.core.models import MEMORY_TYPE_CATEGORY, MemoryEntry
from symbiote.core.ports import SessionRecallPort

# ── SessionRecallPort (B-52) ─────────────────────────────────────────────────


class TestSessionRecallPort:
    """Tests for the session recall port definition."""

    def test_port_is_protocol(self) -> None:
        """SessionRecallPort is a Protocol — no instantiation needed."""
        assert hasattr(SessionRecallPort, "search_messages")
        assert hasattr(SessionRecallPort, "search_sessions")

    def test_mock_implementation_satisfies_port(self) -> None:
        """A simple mock class satisfies the protocol."""

        class MockRecall:
            def search_messages(self, query, symbiote_id=None, session_id=None, limit=10):
                return [{"session_id": "s1", "role": "user", "content": query, "timestamp": "now"}]

            def search_sessions(self, query, symbiote_id=None, limit=5):
                return [{"session_id": "s1", "goal": "test", "summary": query}]

        recall = MockRecall()
        results = recall.search_messages("hello")
        assert len(results) == 1
        assert results[0]["content"] == "hello"

    def test_kernel_accepts_session_recall(self) -> None:
        """Kernel.set_session_recall() wires the port."""
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from symbiote.config.models import KernelConfig
        from symbiote.core.kernel import SymbioteKernel

        class StubRecall:
            def search_messages(self, query, **kwargs):
                return []

            def search_sessions(self, query, **kwargs):
                return []

        with TemporaryDirectory() as tmp:
            kernel = SymbioteKernel(KernelConfig(db_path=Path(tmp) / "test.db"))
            assert kernel.session_recall is None

            kernel.set_session_recall(StubRecall())
            assert kernel.session_recall is not None
            kernel.shutdown()


# ── MemoryCategory (B-53) ────────────────────────────────────────────────────


class TestMemoryCategory:
    """Tests for automatic memory categorization."""

    def test_auto_category_from_type(self) -> None:
        """Category is auto-classified from type when not explicitly set."""
        entry = MemoryEntry(
            symbiote_id="s1", type="preference", scope="global",
            content="User prefers dark mode", source="user",
        )
        assert entry.category == "declarative"

    def test_auto_category_procedural(self) -> None:
        entry = MemoryEntry(
            symbiote_id="s1", type="procedural", scope="global",
            content="To deploy, run make deploy", source="reflection",
        )
        assert entry.category == "procedural"

    def test_auto_category_ephemeral(self) -> None:
        entry = MemoryEntry(
            symbiote_id="s1", type="working", scope="session",
            content="Current task data", source="system",
        )
        assert entry.category == "ephemeral"

    def test_auto_category_meta(self) -> None:
        entry = MemoryEntry(
            symbiote_id="s1", type="reflection", scope="global",
            content="Session summary", source="reflection",
        )
        assert entry.category == "meta"

    def test_explicit_category_overrides(self) -> None:
        """Explicitly set category is preserved."""
        entry = MemoryEntry(
            symbiote_id="s1", type="factual", scope="global",
            content="A fact", source="user", category="procedural",
        )
        assert entry.category == "procedural"

    def test_all_types_have_category_mapping(self) -> None:
        """Every valid memory type has a category mapping."""
        valid_types = [
            "working", "session_summary", "relational", "preference",
            "constraint", "factual", "procedural", "decision",
            "reflection", "semantic_note",
        ]
        for t in valid_types:
            assert t in MEMORY_TYPE_CATEGORY, f"Missing category mapping for type: {t}"

    def test_category_persisted_in_store(self) -> None:
        """Category is persisted and retrieved from SQLite."""
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from symbiote.adapters.storage.sqlite import SQLiteAdapter
        from symbiote.core.identity import IdentityManager
        from symbiote.memory.store import MemoryStore

        with TemporaryDirectory() as tmp:
            adapter = SQLiteAdapter(Path(tmp) / "test.db")
            adapter.init_schema()

            # Create a symbiote to satisfy FK constraint
            identity = IdentityManager(adapter)
            sym = identity.create(name="TestBot", role="assistant")

            store = MemoryStore(adapter)
            entry = MemoryEntry(
                symbiote_id=sym.id, type="constraint", scope="global",
                content="Never do X", source="user",
            )
            assert entry.category == "declarative"

            store.store(entry)
            retrieved = store.get(entry.id)
            assert retrieved is not None
            assert retrieved.category == "declarative"
            adapter.close()


# ── Context Compaction (B-54) ─────────────────────────────────────────────────


class TestContextCompaction:
    """Tests for tool-loop context compaction."""

    def test_compaction_replaces_old_pairs(self) -> None:
        """Old tool-loop message pairs are replaced with summary."""
        from symbiote.runners.chat import ChatRunner

        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Original user message"},
        ]
        initial_count = len(messages)

        # Simulate 5 loop iterations (10 messages)
        for i in range(5):
            messages.append({"role": "assistant", "content": f"Calling tool {i}"})
            messages.append({"role": "user", "content": f"[Tool result: tool_{i}]\nResult {i}"})

        assert len(messages) == 12  # 2 initial + 10 loop

        ChatRunner._compact_loop_messages(messages, initial_count)

        # Should have: 2 initial + 1 summary + 2 last pair = 5
        assert len(messages) == 5
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "compacted" in messages[2]["content"].lower()
        assert messages[3]["role"] == "assistant"  # last pair
        assert messages[4]["role"] == "user"  # last pair

    def test_no_compaction_below_threshold(self) -> None:
        """No compaction when loop messages are below threshold."""
        from symbiote.runners.chat import ChatRunner

        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "User message"},
        ]
        initial_count = len(messages)

        # Only 2 iterations (4 messages) — below threshold of 4 pairs (8 messages)
        for i in range(2):
            messages.append({"role": "assistant", "content": f"Call {i}"})
            messages.append({"role": "user", "content": f"[Tool result: t{i}]\nR{i}"})

        original_len = len(messages)
        ChatRunner._compact_loop_messages(messages, initial_count)
        assert len(messages) == original_len  # unchanged

    def test_compaction_preserves_last_pair(self) -> None:
        """The most recent assistant+tool_result pair is always preserved."""
        from symbiote.runners.chat import ChatRunner

        messages = [{"role": "system", "content": "sys"}]
        initial_count = 1

        for i in range(6):
            messages.append({"role": "assistant", "content": f"call-{i}"})
            messages.append({"role": "user", "content": f"[Tool result: t{i}]\nresult-{i}"})

        ChatRunner._compact_loop_messages(messages, initial_count)

        # Last pair should be the most recent
        assert "call-5" in messages[-2]["content"]
        assert "result-5" in messages[-1]["content"]

    def test_compaction_summary_contains_tool_ids(self) -> None:
        """The summary references tool IDs from compacted steps."""
        from symbiote.runners.chat import ChatRunner

        messages = [{"role": "user", "content": "do stuff"}]
        initial_count = 1

        for i in range(5):
            messages.append({"role": "assistant", "content": "calling items_get"})
            messages.append({"role": "user", "content": f"[Tool result: items_get]\n{{\"id\": {i}}}"})

        ChatRunner._compact_loop_messages(messages, initial_count)

        summary = messages[1]["content"]
        assert "items_get" in summary

    def test_compaction_integrated_in_run(self) -> None:
        """Verify compaction fires during a real tool-loop run()."""
        from symbiote.core.context import AssembledContext
        from symbiote.environment.descriptors import ToolCallResult
        from symbiote.runners.chat import ChatRunner

        call_count = 0

        class LoopingLLM:
            def complete(self, messages, config=None, tools=None):
                nonlocal call_count
                call_count += 1
                if call_count <= 6:
                    return '```tool_call\n{"tool": "test_tool", "params": {}}\n```'
                return "Done!"

        class MockGateway:
            def execute_tool_calls(self, symbiote_id, session_id, calls):
                return [ToolCallResult(tool_id="test_tool", success=True, output="ok")]

        runner = ChatRunner(LoopingLLM(), tool_gateway=MockGateway())
        ctx = AssembledContext(
            symbiote_id="s1", session_id="sess-1", user_input="run loop",
            tool_loop=True,
        )
        result = runner.run(ctx)
        assert result.success
        assert call_count == 7  # 6 tool calls + 1 final response
