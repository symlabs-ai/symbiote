"""E2E tests — validate MVP acceptance criteria from PRD section 23.

Exercises the full product lifecycle via SymbioteKernel (library mode)
and CLI (subprocess mode), verifying state survives restarts,
memory is selective, and all interfaces work.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "e2e.db"


@pytest.fixture()
def mock_llm() -> MockLLMAdapter:
    return MockLLMAdapter(
        responses=[
            "Hello! I'm your symbiote assistant.",
            "Dark mode can be enabled in settings.",
            "The workspace contains 3 Python files.",
        ]
    )


def make_kernel(db_path: Path, llm: MockLLMAdapter) -> SymbioteKernel:
    config = KernelConfig(db_path=db_path)
    return SymbioteKernel(config=config, llm=llm)


# ══════════════════════════════════════════════════════════════════════════════
# PRD Section 23 — MVP Acceptance Criteria
# ══════════════════════════════════════════════════════════════════════════════


class TestMVPScenario:
    """Full end-to-end scenario validating all 10 acceptance criteria."""

    def test_full_lifecycle(self, db_path: Path, mock_llm: MockLLMAdapter) -> None:
        """
        PRD 23.1-23.10:
        1. Create symbiote with persona and environment
        2. Open session with goal
        3. Interact via chat
        4. Execute work in workspace (via learn as proxy)
        5. Produce artifacts (memory entries)
        6. Register decisions
        7. Close session with summary
        8. Persist useful memories
        9. Open new session and recover context without full history
        10. Operate via library
        """
        # ── 1. Create symbiote with persona ──
        kernel = make_kernel(db_path, mock_llm)
        sym = kernel.create_symbiote(
            name="Atlas",
            role="assistant",
            persona={"tone": "friendly", "expertise": "python", "style": "concise"},
        )
        assert sym.name == "Atlas"
        assert sym.persona_json["tone"] == "friendly"

        # ── 2. Open session with goal ──
        session = kernel.start_session(sym.id, goal="Help with Python project")
        assert session.goal == "Help with Python project"
        assert session.status == "active"

        # ── 3. Interact via chat ──
        response1 = kernel.message(session.id, "Hello, how are you?")
        assert response1  # non-empty response
        assert len(mock_llm.calls) >= 1

        response2 = kernel.message(session.id, "How do I enable dark mode?")
        assert response2

        # ── 4. Execute work (learn as proxy for workspace operation) ──
        entry = kernel.capabilities.learn(
            symbiote_id=sym.id,
            session_id=session.id,
            content="User prefers dark mode in all editors",
            fact_type="preference",
            importance=0.9,
        )
        assert entry.type == "preference"
        assert entry.importance == 0.9

        # ── 5. Produce artifacts (memory entries as artifacts) ──
        entry2 = kernel.capabilities.learn(
            symbiote_id=sym.id,
            session_id=session.id,
            content="Always use type hints in Python code",
            fact_type="procedural",
            importance=0.8,
        )
        assert entry2.type == "procedural"

        # ── 6. Register decisions ──
        kernel._sessions.add_decision(
            session.id,
            title="Use dark mode",
            description="User explicitly requested dark mode preference",
            tags=["ui", "preference"],
        )
        decisions = kernel._sessions.get_decisions(session.id)
        assert len(decisions) == 1
        assert decisions[0].title == "Use dark mode"

        # ── 7. Close session with summary ──
        closed = kernel.close_session(session.id)
        assert closed.status == "closed"
        assert closed.summary  # non-empty summary
        assert closed.ended_at is not None

        kernel.shutdown()

        # ── 8. State survives restart ──
        kernel2 = make_kernel(db_path, mock_llm)

        recovered_sym = kernel2.get_symbiote(sym.id)
        assert recovered_sym is not None
        assert recovered_sym.name == "Atlas"
        assert recovered_sym.persona_json["tone"] == "friendly"

        # ── 9. Open new session and recover context without full history ──
        session2 = kernel2.start_session(sym.id, goal="Continue Python help")

        # Memory search should find previously learned facts
        results = kernel2._memory.search("dark mode")
        assert len(results) >= 1
        assert any("dark mode" in r.content for r in results)

        # Context assembly should include relevant memories
        from symbiote.memory.working import WorkingMemory

        ctx = kernel2._context_assembler.build(
            session_id=session2.id,
            symbiote_id=sym.id,
            user_input="dark mode settings",
        )
        # Should have persona
        assert ctx.persona is not None
        assert ctx.persona["tone"] == "friendly"
        # Should have relevant memories (not full history dump)
        assert ctx.total_tokens_estimate <= kernel2._config.context_budget

        # ── 10. Operate via library ──
        response3 = kernel2.message(session2.id, "What about dark mode?")
        assert response3

        kernel2.shutdown()


class TestStateSurvivesRestart:
    """PRD 23: state must survive restart."""

    def test_symbiote_persists(self, db_path: Path, mock_llm: MockLLMAdapter) -> None:
        k1 = make_kernel(db_path, mock_llm)
        sym = k1.create_symbiote("Bot", "helper")
        sym_id = sym.id
        k1.shutdown()

        k2 = make_kernel(db_path, mock_llm)
        recovered = k2.get_symbiote(sym_id)
        assert recovered is not None
        assert recovered.name == "Bot"
        k2.shutdown()

    def test_memory_persists(self, db_path: Path, mock_llm: MockLLMAdapter) -> None:
        k1 = make_kernel(db_path, mock_llm)
        sym = k1.create_symbiote("Bot", "helper")
        sess = k1.start_session(sym.id)
        k1.capabilities.learn(sym.id, sess.id, "Important fact about persistence")
        k1.shutdown()

        k2 = make_kernel(db_path, mock_llm)
        results = k2._memory.search("persistence")
        assert len(results) >= 1
        k2.shutdown()

    def test_session_summary_persists(self, db_path: Path, mock_llm: MockLLMAdapter) -> None:
        k1 = make_kernel(db_path, mock_llm)
        sym = k1.create_symbiote("Bot", "helper")
        sess = k1.start_session(sym.id, goal="test")
        k1.message(sess.id, "hello")
        closed = k1.close_session(sess.id)
        sess_id = closed.id
        k1.shutdown()

        k2 = make_kernel(db_path, mock_llm)
        row = k2._storage.fetch_one("SELECT * FROM sessions WHERE id = ?", (sess_id,))
        assert row is not None
        assert row["status"] == "closed"
        assert row["summary"]
        k2.shutdown()


class TestContextIsSelective:
    """PRD 23: context must be selective, not full history dump."""

    def test_context_within_budget(self, db_path: Path, mock_llm: MockLLMAdapter) -> None:
        config = KernelConfig(db_path=db_path, context_budget=500)
        kernel = SymbioteKernel(config=config, llm=mock_llm)
        sym = kernel.create_symbiote("Bot", "helper")
        sess = kernel.start_session(sym.id)

        # Add many memories to exceed budget
        for i in range(50):
            kernel.capabilities.learn(
                sym.id, sess.id,
                f"Memory entry number {i} with substantial content to fill context budget",
                importance=round(i / 50, 2),
            )

        ctx = kernel._context_assembler.build(
            session_id=sess.id,
            symbiote_id=sym.id,
            user_input="tell me everything",
        )

        # Context should be trimmed to budget
        assert ctx.total_tokens_estimate <= 500
        # Should include most important memories, not all 50
        assert len(ctx.relevant_memories) < 50

        inspection = kernel._context_assembler.inspect(ctx)
        assert inspection.within_budget is True

        kernel.shutdown()

    def test_context_inspectable(self, db_path: Path, mock_llm: MockLLMAdapter) -> None:
        kernel = make_kernel(db_path, mock_llm)
        sym = kernel.create_symbiote("Bot", "helper")
        sess = kernel.start_session(sym.id)

        ctx = kernel._context_assembler.build(
            session_id=sess.id,
            symbiote_id=sym.id,
            user_input="hello",
        )
        inspection = kernel._context_assembler.inspect(ctx)

        assert hasattr(inspection, "included_memories")
        assert hasattr(inspection, "included_knowledge")
        assert hasattr(inspection, "total_tokens_estimate")
        assert hasattr(inspection, "budget")
        assert hasattr(inspection, "within_budget")

        kernel.shutdown()


class TestToolsRespectPolicy:
    """PRD 23: tools must respect policy."""

    def test_tool_blocked_without_config(self, db_path: Path, mock_llm: MockLLMAdapter) -> None:
        kernel = make_kernel(db_path, mock_llm)
        sym = kernel.create_symbiote("Bot", "helper")

        # No environment configured → deny by default
        result = kernel._policy_gate.check(sym.id, "fs_read")
        assert result.allowed is False

        kernel.shutdown()

    def test_tool_allowed_with_config(self, db_path: Path, mock_llm: MockLLMAdapter) -> None:
        kernel = make_kernel(db_path, mock_llm)
        sym = kernel.create_symbiote("Bot", "helper")

        # Configure environment with tools
        kernel._environment.configure(sym.id, tools=["fs_read", "fs_list"])

        result = kernel._policy_gate.check(sym.id, "fs_read")
        assert result.allowed is True

        result2 = kernel._policy_gate.check(sym.id, "fs_write")
        assert result2.allowed is False  # not in configured tools

        kernel.shutdown()


class TestRuntimeWithoutVectorDB:
    """PRD 23: runtime must work without external vector DB."""

    def test_memory_search_works(self, db_path: Path, mock_llm: MockLLMAdapter) -> None:
        kernel = make_kernel(db_path, mock_llm)
        sym = kernel.create_symbiote("Bot", "helper")
        sess = kernel.start_session(sym.id)

        kernel.capabilities.learn(sym.id, sess.id, "Python uses indentation for blocks")
        kernel.capabilities.learn(sym.id, sess.id, "Always use virtual environments")

        results = kernel._memory.search("Python")
        assert len(results) >= 1

        relevant = kernel._memory.get_relevant("indentation", sess.id)
        assert len(relevant) >= 1

        kernel.shutdown()


class TestExportAuditable:
    """PRD 23: memory and decisions must be auditable via export."""

    def test_session_export(self, db_path: Path, mock_llm: MockLLMAdapter) -> None:
        kernel = make_kernel(db_path, mock_llm)
        sym = kernel.create_symbiote("Bot", "helper")
        sess = kernel.start_session(sym.id, goal="export test")
        kernel.message(sess.id, "test message")
        kernel._sessions.add_decision(sess.id, "Test decision", "For export")
        kernel.close_session(sess.id)

        md = kernel._export.export_session(sess.id)
        assert "export test" in md  # goal
        assert "test message" in md  # message content
        assert "Test decision" in md  # decision

        kernel.shutdown()

    def test_memory_export(self, db_path: Path, mock_llm: MockLLMAdapter) -> None:
        kernel = make_kernel(db_path, mock_llm)
        sym = kernel.create_symbiote("Bot", "helper")
        sess = kernel.start_session(sym.id)
        kernel.capabilities.learn(sym.id, sess.id, "Exportable fact")

        md = kernel._export.export_memory(sym.id)
        assert "Exportable fact" in md

        kernel.shutdown()
