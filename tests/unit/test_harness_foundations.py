"""Tests for Harness Foundations — Fase 1 of Meta-Harness evolution plan.

Covers:
  H-01: SessionScore (auto_score computation)
  H-02: FeedbackPort (user feedback integration)
  H-03: MemoryEntry de falha determinística
  H-04: Context splits configuráveis
  H-05: LoopTrace persistence
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.context import AssembledContext, ContextAssembler
from symbiote.core.identity import IdentityManager
from symbiote.core.models import EnvironmentConfig, MemoryEntry
from symbiote.core.scoring import (
    SessionScore,
    compute_auto_score,
    compute_final_score,
)
from symbiote.environment.manager import EnvironmentManager
from symbiote.knowledge.service import KnowledgeService
from symbiote.memory.store import MemoryStore
from symbiote.runners.base import LoopStep, LoopTrace

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "harness_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name="HarnessBot", role="assistant")
    return sym.id


@pytest.fixture()
def env_manager(adapter: SQLiteAdapter) -> EnvironmentManager:
    return EnvironmentManager(storage=adapter)


@pytest.fixture()
def memory_store(adapter: SQLiteAdapter) -> MemoryStore:
    return MemoryStore(storage=adapter)


# ── H-01: SessionScore ──────────────────────────────────────────────────────


class TestComputeAutoScore:
    def test_none_trace_returns_0_8(self) -> None:
        assert compute_auto_score(None) == 0.8

    def test_end_turn_1_iteration(self) -> None:
        trace = LoopTrace(
            steps=[LoopStep(iteration=1, tool_id="t", success=True)],
            total_iterations=1, total_tool_calls=1, stop_reason="end_turn",
        )
        assert compute_auto_score(trace) == 1.0

    def test_end_turn_2_iterations(self) -> None:
        trace = LoopTrace(
            steps=[
                LoopStep(iteration=1, tool_id="t", success=True),
                LoopStep(iteration=2, tool_id="t", success=True),
            ],
            total_iterations=2, total_tool_calls=2, stop_reason="end_turn",
        )
        assert compute_auto_score(trace) == 1.0  # <= 2 iters = 1.0

    def test_end_turn_3_iterations_no_penalty(self) -> None:
        """3 iterations in brief mode = no penalty (multi-step is normal)."""
        trace = LoopTrace(
            steps=[LoopStep(iteration=i, tool_id="t", success=True) for i in range(1, 4)],
            total_iterations=3, total_tool_calls=3, stop_reason="end_turn",
        )
        assert compute_auto_score(trace) == 1.0

    def test_end_turn_6_iterations_moderate_penalty(self) -> None:
        """6 iterations in brief mode = moderate penalty (0.85 factor)."""
        trace = LoopTrace(
            steps=[LoopStep(iteration=i, tool_id="t", success=True) for i in range(1, 7)],
            total_iterations=6, total_tool_calls=6, stop_reason="end_turn",
        )
        assert compute_auto_score(trace) == 0.85

    def test_stagnation(self) -> None:
        trace = LoopTrace(
            steps=[LoopStep(iteration=1, tool_id="t", success=True)],
            total_iterations=2, stop_reason="stagnation",
        )
        assert compute_auto_score(trace) == 0.2

    def test_circuit_breaker(self) -> None:
        trace = LoopTrace(
            steps=[LoopStep(iteration=i, tool_id="t", success=False) for i in range(1, 4)],
            total_iterations=3, stop_reason="circuit_breaker",
        )
        # 0.1 base * (1 - 1.0 * 0.3) = 0.1 * 0.7 = 0.07
        assert compute_auto_score(trace) == 0.07

    def test_max_iterations(self) -> None:
        trace = LoopTrace(
            steps=[LoopStep(iteration=1, tool_id="t", success=True)],
            total_iterations=10, stop_reason="max_iterations",
        )
        assert compute_auto_score(trace) == 0.0

    def test_tool_failures_penalize(self) -> None:
        trace = LoopTrace(
            steps=[
                LoopStep(iteration=1, tool_id="a", success=True),
                LoopStep(iteration=2, tool_id="b", success=False),
            ],
            total_iterations=2, total_tool_calls=2, stop_reason="end_turn",
        )
        # base 1.0 * iter_factor 1.0 * (1 - 0.5 * 0.3) = 0.85
        assert compute_auto_score(trace) == 0.85

    def test_all_failures(self) -> None:
        trace = LoopTrace(
            steps=[
                LoopStep(iteration=1, tool_id="t", success=False),
                LoopStep(iteration=2, tool_id="t", success=False),
            ],
            total_iterations=2, stop_reason="stagnation",
        )
        # 0.2 * (1 - 1.0 * 0.3) = 0.14
        assert compute_auto_score(trace) == 0.14


class TestComputeFinalScore:
    def test_no_user_score(self) -> None:
        assert compute_final_score(0.8) == 0.8

    def test_with_user_score(self) -> None:
        # 0.8 * 0.6 + 1.0 * 0.4 = 0.48 + 0.4 = 0.88
        assert compute_final_score(0.8, 1.0) == 0.88

    def test_low_user_score(self) -> None:
        # 1.0 * 0.6 + 0.0 * 0.4 = 0.6
        assert compute_final_score(1.0, 0.0) == 0.6

    def test_both_zero(self) -> None:
        assert compute_final_score(0.0, 0.0) == 0.0


class TestSessionScoreModel:
    def test_creation(self) -> None:
        score = SessionScore(session_id="s1", symbiote_id="sym1", auto_score=0.8, final_score=0.8)
        assert score.session_id == "s1"
        assert score.auto_score == 0.8
        assert score.user_score is None
        assert score.id  # UUID generated


# ── H-02: FeedbackPort ──────────────────────────────────────────────────────


class TestFeedbackPort:
    def test_report_feedback_updates_score(self, adapter: SQLiteAdapter, symbiote_id: str) -> None:
        from symbiote.config.models import KernelConfig
        from symbiote.core.kernel import SymbioteKernel

        config = KernelConfig(db_path=adapter._db_path)
        kernel = SymbioteKernel(config)
        session = kernel.start_session(symbiote_id, goal="test")

        # Close session to create initial auto_score
        kernel.close_session(session.id)

        # Report feedback
        kernel.report_feedback(session.id, 0.9, source="user_click")

        # Verify score was updated
        row = kernel._storage.fetch_one(
            "SELECT * FROM session_scores WHERE session_id = ?", (session.id,)
        )
        assert row is not None
        assert row["user_score"] == 0.9
        assert row["final_score"] == round(row["auto_score"] * 0.6 + 0.9 * 0.4, 2)

        kernel.shutdown()

    def test_report_feedback_no_prior_score(self, adapter: SQLiteAdapter, symbiote_id: str) -> None:
        from symbiote.config.models import KernelConfig
        from symbiote.core.kernel import SymbioteKernel

        config = KernelConfig(db_path=adapter._db_path)
        kernel = SymbioteKernel(config)
        session = kernel.start_session(symbiote_id, goal="test")

        # Report feedback WITHOUT closing session first
        kernel.report_feedback(session.id, 0.5)

        row = kernel._storage.fetch_one(
            "SELECT * FROM session_scores WHERE session_id = ?", (session.id,)
        )
        assert row is not None
        assert row["user_score"] == 0.5

        kernel.shutdown()


# ── H-03: MemoryEntry de falha ──────────────────────────────────────────────


class TestFailureMemory:
    def test_circuit_breaker_generates_memory(self, adapter: SQLiteAdapter) -> None:
        from symbiote.config.models import KernelConfig
        from symbiote.core.kernel import SymbioteKernel

        config = KernelConfig(db_path=adapter._db_path)

        class MockLLM:
            def complete(self, messages, config=None, tools=None):
                return "ok"

        kernel = SymbioteKernel(config, llm=MockLLM())
        sym = kernel.create_symbiote(name="FailBot", role="test")
        session = kernel.start_session(sym.id, goal="test")

        # Simulate a circuit breaker trace
        kernel._last_trace = LoopTrace(
            steps=[
                LoopStep(iteration=1, tool_id="flaky_api", success=False, error="timeout"),
                LoopStep(iteration=2, tool_id="flaky_api", success=False, error="timeout"),
                LoopStep(iteration=3, tool_id="flaky_api", success=False, error="timeout"),
            ],
            total_iterations=3, total_tool_calls=3, stop_reason="circuit_breaker",
        )
        kernel._last_trace_session = session.id

        kernel.close_session(session.id)

        # Check that a procedural memory was created
        memories = kernel._memory.search("flaky_api", tags=["harness_failure"])
        assert len(memories) >= 1
        mem = memories[0]
        assert mem.type == "procedural"
        assert "flaky_api" in mem.content
        assert "pré-condições" in mem.content

        kernel.shutdown()

    def test_stagnation_generates_memory(self, adapter: SQLiteAdapter) -> None:
        from symbiote.config.models import KernelConfig
        from symbiote.core.kernel import SymbioteKernel

        config = KernelConfig(db_path=adapter._db_path)
        kernel = SymbioteKernel(config)
        sym = kernel.create_symbiote(name="StagBot", role="test")
        session = kernel.start_session(sym.id, goal="test")

        kernel._last_trace = LoopTrace(
            steps=[
                LoopStep(iteration=1, tool_id="search", success=True),
                LoopStep(iteration=2, tool_id="search", success=True),
            ],
            total_iterations=2, stop_reason="stagnation",
        )
        kernel._last_trace_session = session.id

        kernel.close_session(session.id)

        memories = kernel._memory.search("search", tags=["harness_failure"])
        assert len(memories) >= 1
        assert "estagnou" in memories[0].content

        kernel.shutdown()

    def test_max_iterations_generates_memory(self, adapter: SQLiteAdapter) -> None:
        from symbiote.config.models import KernelConfig
        from symbiote.core.kernel import SymbioteKernel

        config = KernelConfig(db_path=adapter._db_path)
        kernel = SymbioteKernel(config)
        sym = kernel.create_symbiote(name="MaxBot", role="test")
        session = kernel.start_session(sym.id, goal="test")

        kernel._last_trace = LoopTrace(
            steps=[LoopStep(iteration=i, tool_id=f"tool_{i % 3}", success=True) for i in range(10)],
            total_iterations=10, total_tool_calls=10, stop_reason="max_iterations",
        )
        kernel._last_trace_session = session.id

        kernel.close_session(session.id)

        memories = kernel._memory.search("esgotou", tags=["harness_failure"])
        assert len(memories) >= 1
        assert "10 iterações" in memories[0].content

        kernel.shutdown()

    def test_end_turn_no_memory(self, adapter: SQLiteAdapter) -> None:
        from symbiote.config.models import KernelConfig
        from symbiote.core.kernel import SymbioteKernel

        config = KernelConfig(db_path=adapter._db_path)
        kernel = SymbioteKernel(config)
        sym = kernel.create_symbiote(name="OkBot", role="test")
        session = kernel.start_session(sym.id, goal="test")

        kernel._last_trace = LoopTrace(
            steps=[LoopStep(iteration=1, tool_id="t", success=True)],
            total_iterations=1, stop_reason="end_turn",
        )
        kernel._last_trace_session = session.id

        kernel.close_session(session.id)

        memories = kernel._memory.search("harness_failure", tags=["harness_failure"])
        assert len(memories) == 0

        kernel.shutdown()

    def test_no_trace_no_memory(self, adapter: SQLiteAdapter) -> None:
        from symbiote.config.models import KernelConfig
        from symbiote.core.kernel import SymbioteKernel

        config = KernelConfig(db_path=adapter._db_path)
        kernel = SymbioteKernel(config)
        sym = kernel.create_symbiote(name="NoTraceBot", role="test")
        session = kernel.start_session(sym.id, goal="test")

        # No trace set
        kernel.close_session(session.id)

        memories = kernel._memory.search("harness_failure", tags=["harness_failure"])
        assert len(memories) == 0

        kernel.shutdown()


# ── H-04: Context splits configuráveis ──────────────────────────────────────


class TestContextSplits:
    def test_default_splits(self, env_manager: EnvironmentManager, symbiote_id: str) -> None:
        assert env_manager.get_memory_share(symbiote_id) == 0.40
        assert env_manager.get_knowledge_share(symbiote_id) == 0.25

    def test_custom_splits_persist(self, env_manager: EnvironmentManager, symbiote_id: str) -> None:
        env_manager.configure(
            symbiote_id=symbiote_id,
            memory_share=0.60,
            knowledge_share=0.10,
        )
        assert env_manager.get_memory_share(symbiote_id) == 0.60
        assert env_manager.get_knowledge_share(symbiote_id) == 0.10

    def test_update_splits(self, env_manager: EnvironmentManager, symbiote_id: str) -> None:
        env_manager.configure(symbiote_id=symbiote_id, memory_share=0.50)
        env_manager.configure(symbiote_id=symbiote_id, knowledge_share=0.30)
        assert env_manager.get_memory_share(symbiote_id) == 0.50
        assert env_manager.get_knowledge_share(symbiote_id) == 0.30

    def test_splits_in_env_config_model(self) -> None:
        cfg = EnvironmentConfig(symbiote_id="test", memory_share=0.7, knowledge_share=0.2)
        assert cfg.memory_share == 0.7
        assert cfg.knowledge_share == 0.2

    def test_default_env_config_model(self) -> None:
        cfg = EnvironmentConfig(symbiote_id="test")
        assert cfg.memory_share == 0.40
        assert cfg.knowledge_share == 0.25


# ── H-05: LoopTrace persistence ─────────────────────────────────────────────


class TestTracePersistence:
    def test_schema_creates_tables(self, adapter: SQLiteAdapter) -> None:
        tables = adapter.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('execution_traces', 'session_scores')"
        )
        names = {t["name"] for t in tables}
        assert "execution_traces" in names
        assert "session_scores" in names

    def test_persist_and_retrieve_trace(self, adapter: SQLiteAdapter) -> None:
        trace = LoopTrace(
            steps=[
                LoopStep(iteration=1, tool_id="search", params={"q": "test"}, success=True, elapsed_ms=100),
                LoopStep(iteration=2, tool_id="publish", success=False, error="denied", elapsed_ms=50),
            ],
            total_iterations=2, total_tool_calls=2, total_elapsed_ms=150, stop_reason="circuit_breaker",
        )

        from datetime import UTC, datetime
        from uuid import uuid4

        trace_id = str(uuid4())
        adapter.execute(
            "INSERT INTO execution_traces "
            "(id, session_id, symbiote_id, total_iterations, total_tool_calls, "
            "total_elapsed_ms, stop_reason, steps_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trace_id, "sess-1", "sym-1",
                trace.total_iterations, trace.total_tool_calls,
                trace.total_elapsed_ms, trace.stop_reason,
                json.dumps([s.model_dump() for s in trace.steps]),
                datetime.now(tz=UTC).isoformat(),
            ),
        )

        row = adapter.fetch_one("SELECT * FROM execution_traces WHERE id = ?", (trace_id,))
        assert row is not None
        assert row["stop_reason"] == "circuit_breaker"
        assert row["total_iterations"] == 2

        steps = json.loads(row["steps_json"])
        assert len(steps) == 2
        assert steps[0]["tool_id"] == "search"
        assert steps[1]["success"] is False

    def test_persist_and_retrieve_score(self, adapter: SQLiteAdapter) -> None:
        from datetime import UTC, datetime
        from uuid import uuid4

        score_id = str(uuid4())
        adapter.execute(
            "INSERT INTO session_scores "
            "(id, session_id, symbiote_id, auto_score, final_score, "
            "stop_reason, total_iterations, total_tool_calls, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                score_id, "sess-1", "sym-1",
                0.7, 0.7, "end_turn", 3, 3,
                datetime.now(tz=UTC).isoformat(),
            ),
        )

        row = adapter.fetch_one("SELECT * FROM session_scores WHERE id = ?", (score_id,))
        assert row is not None
        assert row["auto_score"] == 0.7
        assert row["stop_reason"] == "end_turn"


# ── Integration: kernel persists trace and score on close ────────────────────


class TestKernelIntegration:
    def test_close_session_persists_score(self, adapter: SQLiteAdapter) -> None:
        from symbiote.config.models import KernelConfig
        from symbiote.core.kernel import SymbioteKernel

        config = KernelConfig(db_path=adapter._db_path)
        kernel = SymbioteKernel(config)
        sym = kernel.create_symbiote(name="IntBot", role="test")
        session = kernel.start_session(sym.id, goal="test")

        # Simulate trace
        kernel._last_trace = LoopTrace(
            steps=[LoopStep(iteration=1, tool_id="t", success=True)],
            total_iterations=1, total_tool_calls=1, stop_reason="end_turn",
        )
        kernel._last_trace_session = session.id

        kernel.close_session(session.id)

        # Verify score persisted
        score_row = kernel._storage.fetch_one(
            "SELECT * FROM session_scores WHERE session_id = ?", (session.id,)
        )
        assert score_row is not None
        assert score_row["auto_score"] == 1.0
        assert score_row["stop_reason"] == "end_turn"

        # Verify trace state cleared
        assert kernel._last_trace is None

        kernel.shutdown()

    def test_close_session_persists_trace(self, adapter: SQLiteAdapter) -> None:
        """Trace is persisted in message(), not close_session.
        But we verify the full flow here via _persist_trace."""
        from symbiote.config.models import KernelConfig
        from symbiote.core.kernel import SymbioteKernel

        config = KernelConfig(db_path=adapter._db_path)
        kernel = SymbioteKernel(config)
        sym = kernel.create_symbiote(name="TraceBot", role="test")
        session = kernel.start_session(sym.id, goal="test")

        trace = LoopTrace(
            steps=[LoopStep(iteration=1, tool_id="search", success=True, elapsed_ms=200)],
            total_iterations=1, total_tool_calls=1, total_elapsed_ms=200, stop_reason="end_turn",
        )

        # Persist trace directly (normally done in _message_inner)
        kernel._persist_trace(session.id, sym.id, trace)

        rows = kernel._storage.fetch_all(
            "SELECT * FROM execution_traces WHERE session_id = ?", (session.id,)
        )
        assert len(rows) == 1
        assert rows[0]["stop_reason"] == "end_turn"
        assert rows[0]["total_elapsed_ms"] == 200

        kernel.shutdown()
