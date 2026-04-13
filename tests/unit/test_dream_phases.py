"""Tests for Dream Mode phases."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.models import MemoryEntry
from symbiote.dream.models import BudgetTracker, DreamContext
from symbiote.dream.phases import (
    EvaluatePhase,
    GeneralizePhase,
    MinePhase,
    PrunePhase,
    ReconcilePhase,
)
from symbiote.memory.store import MemoryStore


def _uuid() -> str:
    return str(uuid4())


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "dream_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    # Insert a symbiote for FK constraints
    adp.execute(
        "INSERT INTO symbiotes (id, name, role, created_at) VALUES (?, ?, ?, ?)",
        ("sym-1", "test-agent", "assistant", _utcnow().isoformat()),
    )
    yield adp
    adp.close()


@pytest.fixture()
def memory(adapter: SQLiteAdapter) -> MemoryStore:
    return MemoryStore(adapter)


@pytest.fixture()
def ctx(adapter: SQLiteAdapter, memory: MemoryStore) -> DreamContext:
    return DreamContext(
        symbiote_id="sym-1",
        storage=adapter,
        memory=memory,
        llm=None,
        budget=BudgetTracker(10),
        dry_run=False,
    )


def _make_entry(
    *,
    symbiote_id: str = "sym-1",
    content: str = "test memory",
    entry_type: str = "factual",
    importance: float = 0.5,
    tags: list[str] | None = None,
    days_ago: float = 0,
) -> MemoryEntry:
    last_used = _utcnow() - timedelta(days=days_ago)
    return MemoryEntry(
        symbiote_id=symbiote_id,
        type=entry_type,
        scope="global",
        content=content,
        tags=tags or [],
        importance=importance,
        source="user",
        confidence=1.0,
        last_used_at=last_used,
    )


# ── PrunePhase tests ───────────────────────────────────────────────────────


class TestPrunePhase:
    def test_prunes_stale_low_importance_memories(self, ctx, memory):
        # decay = 60 days * (1 - 0.3) = 42 > 30 threshold
        entry = _make_entry(importance=0.3, days_ago=60)
        memory.store(entry)

        result = PrunePhase().run(ctx)

        assert result.phase == "prune"
        assert result.actions_proposed == 1
        assert result.actions_applied == 1

    def test_keeps_recent_memories(self, ctx, memory):
        entry = _make_entry(importance=0.3, days_ago=5)
        memory.store(entry)

        result = PrunePhase().run(ctx)

        assert result.actions_proposed == 0
        assert result.actions_applied == 0

    def test_keeps_high_importance_memories(self, ctx, memory):
        # decay = 60 * (1 - 0.9) = 6 < 30
        entry = _make_entry(importance=0.9, days_ago=60)
        memory.store(entry)

        result = PrunePhase().run(ctx)

        assert result.actions_proposed == 0

    def test_protects_constraint_type(self, ctx, memory):
        # Even with high decay, constraints are protected
        entry = _make_entry(entry_type="constraint", importance=0.1, days_ago=100)
        memory.store(entry)

        result = PrunePhase().run(ctx)

        assert result.actions_proposed == 0

    def test_dry_run_proposes_without_applying(self, ctx, memory):
        ctx.dry_run = True
        entry = _make_entry(importance=0.2, days_ago=60)
        mid = memory.store(entry)

        result = PrunePhase().run(ctx)

        assert result.actions_proposed == 1
        assert result.actions_applied == 0
        # Memory should still be active
        fetched = memory.get(mid)
        assert fetched is not None
        assert fetched.is_active is True


# ── ReconcilePhase tests ───────────────────────────────────────────────────


class TestReconcilePhase:
    def test_detects_conflicting_memories(self, ctx, memory):
        # Same tags, very different content — overlap 3/3 = 1.0 > 0.6
        memory.store(_make_entry(
            content="launch typora using nohup typora in background",
            tags=["app_launch", "typora", "gui"],
            importance=0.7,
        ))
        memory.store(_make_entry(
            content="use flatpak run com.typora.Typora for opening the editor",
            tags=["app_launch", "typora", "gui"],
            importance=0.9,
        ))

        result = ReconcilePhase().run(ctx)

        assert result.phase == "reconcile"
        assert result.actions_proposed == 1
        assert result.actions_applied == 1
        # The lower-importance entry should be deactivated
        assert result.details[0]["loser_importance"] == 0.7

    def test_no_conflict_when_tags_differ(self, ctx, memory):
        memory.store(_make_entry(
            content="open firefox browser",
            tags=["browser", "firefox"],
            importance=0.7,
        ))
        memory.store(_make_entry(
            content="compile rust project",
            tags=["rust", "build"],
            importance=0.8,
        ))

        result = ReconcilePhase().run(ctx)

        assert result.actions_proposed == 0

    def test_no_conflict_when_content_similar(self, ctx, memory):
        # Same tags AND similar content = not a conflict (they agree)
        memory.store(_make_entry(
            content="open typora with nohup background",
            tags=["app_launch", "typora", "gui"],
            importance=0.7,
        ))
        memory.store(_make_entry(
            content="open typora with nohup in background",
            tags=["app_launch", "typora", "gui"],
            importance=0.8,
        ))

        result = ReconcilePhase().run(ctx)

        assert result.actions_proposed == 0

    def test_dry_run_reports_without_action(self, ctx, memory):
        ctx.dry_run = True
        m1 = memory.store(_make_entry(
            content="launch typora using nohup typora in background",
            tags=["app_launch", "typora", "gui"],
            importance=0.7,
        ))
        memory.store(_make_entry(
            content="use flatpak run com.typora.Typora for opening the editor",
            tags=["app_launch", "typora", "gui"],
            importance=0.9,
        ))

        result = ReconcilePhase().run(ctx)

        assert result.actions_proposed == 1
        assert result.actions_applied == 0
        # Both memories should still be active
        fetched = memory.get(m1)
        assert fetched.is_active is True

    def test_skips_entries_with_few_tags(self, ctx, memory):
        # Entries with < 2 tags are ignored
        memory.store(_make_entry(content="something", tags=["solo"], importance=0.5))
        memory.store(_make_entry(content="different", tags=["solo"], importance=0.8))

        result = ReconcilePhase().run(ctx)

        assert result.actions_proposed == 0


# ── Mock LLM ────────────────────────────────────────────────────────────────


class MockLLM:
    """Mock LLM that returns configurable responses."""

    def __init__(self, response: str = "Mock response") -> None:
        self.calls: list[list[dict]] = []
        self._response = response

    def complete(self, messages: list[dict], config=None, tools=None) -> str:
        self.calls.append(messages)
        return self._response


@pytest.fixture()
def llm_ctx(adapter: SQLiteAdapter, memory: MemoryStore) -> DreamContext:
    return DreamContext(
        symbiote_id="sym-1",
        storage=adapter,
        memory=memory,
        llm=MockLLM(),
        budget=BudgetTracker(10),
        dry_run=False,
    )


# ── GeneralizePhase tests ──────────────────────────────────────────────────


class TestGeneralizePhase:
    def test_generalizes_cluster_of_3(self, adapter, memory):
        llm = MockLLM(response="For GUI apps, use nohup <app> >/dev/null 2>&1 &")
        ctx = DreamContext(
            symbiote_id="sym-1",
            storage=adapter,
            memory=memory,
            llm=llm,
            budget=BudgetTracker(10),
            dry_run=False,
        )
        # 3 similar procedural memories with shared tags
        for app in ["typora", "firefox", "code"]:
            memory.store(_make_entry(
                content=f"launch {app} with nohup {app} in background",
                entry_type="procedural",
                tags=["app_launch", "gui", "nohup"],
                importance=0.7,
            ))

        result = GeneralizePhase().run(ctx)

        assert result.phase == "generalize"
        assert result.actions_proposed >= 1
        assert result.actions_applied >= 1
        assert len(llm.calls) == 1

    def test_skips_small_clusters(self, llm_ctx, memory):
        # Only 2 entries — not enough for generalization
        for app in ["typora", "firefox"]:
            memory.store(_make_entry(
                content=f"launch {app}",
                entry_type="procedural",
                tags=["app_launch", "gui", "nohup"],
                importance=0.7,
            ))

        result = GeneralizePhase().run(llm_ctx)

        assert result.actions_proposed == 0

    def test_respects_budget(self, adapter, memory):
        llm = MockLLM(response="generalized rule")
        ctx = DreamContext(
            symbiote_id="sym-1",
            storage=adapter,
            memory=memory,
            llm=llm,
            budget=BudgetTracker(0),  # no budget
            dry_run=False,
        )
        for i in range(4):
            memory.store(_make_entry(
                content=f"procedure {i}",
                entry_type="procedural",
                tags=["step", "common", "pattern"],
                importance=0.5,
            ))

        GeneralizePhase().run(ctx)

        assert len(llm.calls) == 0  # budget exhausted, no LLM calls

    def test_no_llm_returns_error(self, ctx, memory):
        result = GeneralizePhase().run(ctx)
        assert result.error == "no_llm"


# ── MinePhase tests ─────────────────────────────────────────────────────────


class TestMinePhase:
    def test_mines_failure_patterns(self, adapter, memory):
        llm = MockLLM(response=json.dumps([
            {"content": "Avoid calling bash without checking exit code", "importance": 0.8},
        ]))
        ctx = DreamContext(
            symbiote_id="sym-1",
            storage=adapter,
            memory=memory,
            llm=llm,
            budget=BudgetTracker(10),
            dry_run=False,
        )
        # Insert a failed execution trace
        adapter.execute(
            "INSERT INTO execution_traces "
            "(id, session_id, symbiote_id, stop_reason, steps_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                _uuid(), _uuid(), "sym-1", "circuit_breaker",
                json.dumps([{"tool_id": "bash", "success": False, "error": "timeout"}]),
                _utcnow().isoformat(),
            ),
        )

        result = MinePhase().run(ctx)

        assert result.phase == "mine"
        assert result.actions_applied >= 1
        assert result.llm_calls_used == 1

    def test_no_traces_returns_empty(self, llm_ctx):
        result = MinePhase().run(llm_ctx)
        assert result.actions_proposed == 0
        assert result.llm_calls_used == 0

    def test_no_llm_returns_error(self, ctx):
        result = MinePhase().run(ctx)
        assert result.error == "no_llm"


# ── EvaluatePhase tests ────────────────────────────────────────────────────


class TestEvaluatePhase:
    def test_reviews_low_scoring_sessions(self, adapter, memory):
        llm = MockLLM(response="The agent should verify tool output before responding")
        ctx = DreamContext(
            symbiote_id="sym-1",
            storage=adapter,
            memory=memory,
            llm=llm,
            budget=BudgetTracker(10),
            dry_run=False,
        )
        # Insert a low-scoring session with messages
        sid = _uuid()
        adapter.execute(
            "INSERT INTO sessions (id, symbiote_id, status) VALUES (?, ?, 'closed')",
            (sid, "sym-1"),
        )
        adapter.execute(
            "INSERT INTO session_scores "
            "(id, session_id, symbiote_id, final_score, computed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (_uuid(), sid, "sym-1", 0.2, _utcnow().isoformat()),
        )
        adapter.execute(
            "INSERT INTO messages (id, session_id, role, content, created_at) "
            "VALUES (?, ?, 'user', 'open typora', ?)",
            (_uuid(), sid, _utcnow().isoformat()),
        )

        result = EvaluatePhase().run(ctx)

        assert result.phase == "evaluate"
        assert result.actions_applied >= 1
        assert result.llm_calls_used >= 1

    def test_skips_high_scoring_sessions(self, llm_ctx, adapter):
        sid = _uuid()
        adapter.execute(
            "INSERT INTO sessions (id, symbiote_id, status) VALUES (?, ?, 'closed')",
            (sid, "sym-1"),
        )
        adapter.execute(
            "INSERT INTO session_scores "
            "(id, session_id, symbiote_id, final_score, computed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (_uuid(), sid, "sym-1", 0.9, _utcnow().isoformat()),
        )

        result = EvaluatePhase().run(llm_ctx)

        assert result.actions_proposed == 0

    def test_no_llm_returns_error(self, ctx):
        result = EvaluatePhase().run(ctx)
        assert result.error == "no_llm"
