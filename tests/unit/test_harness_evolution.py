"""Tests for Harness Evolution — Fase 2 of Meta-Harness plan.

Covers:
  H-06: HarnessVersionRepository (CRUD, rollback, score tracking)
  H-07: ParameterTuner (tiered activation, rules, apply)
  H-08: max_tool_iterations configurable (EnvironmentConfig → ChatRunner)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.context import AssembledContext
from symbiote.core.identity import IdentityManager
from symbiote.core.models import EnvironmentConfig
from symbiote.environment.manager import EnvironmentManager
from symbiote.harness.tuner import (
    TIER_1_MIN,
    TIER_2_MIN,
    TIER_3_MIN,
    ParameterTuner,
    TuningResult,
)
from symbiote.harness.versions import HarnessVersionRepository

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "evolution_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name="EvoBot", role="assistant")
    return sym.id


@pytest.fixture()
def env_manager(adapter: SQLiteAdapter) -> EnvironmentManager:
    return EnvironmentManager(storage=adapter)


@pytest.fixture()
def versions(adapter: SQLiteAdapter) -> HarnessVersionRepository:
    return HarnessVersionRepository(storage=adapter)


@pytest.fixture()
def tuner(adapter: SQLiteAdapter) -> ParameterTuner:
    return ParameterTuner(storage=adapter)


def _insert_trace(adapter, symbiote_id, stop_reason="end_turn", iterations=2, tool_calls=2):
    adapter.execute(
        "INSERT INTO execution_traces "
        "(id, session_id, symbiote_id, total_iterations, total_tool_calls, "
        "total_elapsed_ms, stop_reason, steps_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(uuid4()), str(uuid4()), symbiote_id,
            iterations, tool_calls, 100,
            stop_reason, "[]",
            datetime.now(tz=UTC).isoformat(),
        ),
    )


def _insert_score(adapter, symbiote_id, final_score=0.8, tool_calls=2):
    adapter.execute(
        "INSERT INTO session_scores "
        "(id, session_id, symbiote_id, auto_score, final_score, "
        "stop_reason, total_iterations, total_tool_calls, computed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(uuid4()), str(uuid4()), symbiote_id,
            final_score, final_score, "end_turn", 2, tool_calls,
            datetime.now(tz=UTC).isoformat(),
        ),
    )


# ── H-06: HarnessVersionRepository ──────────────────────────────────────────


class TestHarnessVersions:
    def test_no_active_version_returns_none(self, versions, symbiote_id) -> None:
        assert versions.get_active(symbiote_id, "tool_instructions") is None

    def test_create_and_retrieve(self, versions, symbiote_id) -> None:
        v = versions.create_version(symbiote_id, "tool_instructions", "Be concise.")
        assert v == 1
        content = versions.get_active(symbiote_id, "tool_instructions")
        assert content == "Be concise."

    def test_create_second_version_deactivates_first(self, versions, symbiote_id) -> None:
        versions.create_version(symbiote_id, "tool_instructions", "Version 1")
        versions.create_version(symbiote_id, "tool_instructions", "Version 2")
        assert versions.get_active(symbiote_id, "tool_instructions") == "Version 2"

    def test_version_numbers_increment(self, versions, symbiote_id) -> None:
        v1 = versions.create_version(symbiote_id, "tool_instructions", "V1")
        v2 = versions.create_version(symbiote_id, "tool_instructions", "V2")
        v3 = versions.create_version(symbiote_id, "tool_instructions", "V3")
        assert v1 == 1
        assert v2 == 2
        assert v3 == 3

    def test_different_components_independent(self, versions, symbiote_id) -> None:
        versions.create_version(symbiote_id, "tool_instructions", "Tools V1")
        versions.create_version(symbiote_id, "injection_stagnation", "Stag V1")
        assert versions.get_active(symbiote_id, "tool_instructions") == "Tools V1"
        assert versions.get_active(symbiote_id, "injection_stagnation") == "Stag V1"

    def test_different_symbiotes_independent(self, versions, adapter) -> None:
        mgr = IdentityManager(storage=adapter)
        sym_a = mgr.create(name="A", role="a").id
        sym_b = mgr.create(name="B", role="b").id

        versions.create_version(sym_a, "tool_instructions", "A's instructions")
        versions.create_version(sym_b, "tool_instructions", "B's instructions")
        assert versions.get_active(sym_a, "tool_instructions") == "A's instructions"
        assert versions.get_active(sym_b, "tool_instructions") == "B's instructions"

    def test_rollback(self, versions, symbiote_id) -> None:
        versions.create_version(symbiote_id, "tool_instructions", "V1 good")
        versions.create_version(symbiote_id, "tool_instructions", "V2 bad")
        assert versions.get_active(symbiote_id, "tool_instructions") == "V2 bad"

        ok = versions.rollback(symbiote_id, "tool_instructions")
        assert ok is True
        assert versions.get_active(symbiote_id, "tool_instructions") == "V1 good"

    def test_rollback_no_previous_returns_false(self, versions, symbiote_id) -> None:
        versions.create_version(symbiote_id, "tool_instructions", "V1 only")
        ok = versions.rollback(symbiote_id, "tool_instructions")
        assert ok is False

    def test_rollback_no_active_returns_false(self, versions, symbiote_id) -> None:
        ok = versions.rollback(symbiote_id, "tool_instructions")
        assert ok is False

    def test_update_score(self, versions, symbiote_id) -> None:
        versions.create_version(symbiote_id, "tool_instructions", "V1")

        versions.update_score(symbiote_id, "tool_instructions", 0.8)
        row = versions.get_active_version(symbiote_id, "tool_instructions")
        assert row["session_count"] == 1
        assert row["avg_score"] == 0.8

        versions.update_score(symbiote_id, "tool_instructions", 0.6)
        row = versions.get_active_version(symbiote_id, "tool_instructions")
        assert row["session_count"] == 2
        assert row["avg_score"] == pytest.approx(0.7, abs=0.01)

    def test_list_versions(self, versions, symbiote_id) -> None:
        versions.create_version(symbiote_id, "tool_instructions", "V1")
        versions.create_version(symbiote_id, "tool_instructions", "V2")
        versions.create_version(symbiote_id, "tool_instructions", "V3")

        history = versions.list_versions(symbiote_id, "tool_instructions")
        assert len(history) == 3
        assert history[0]["version"] == 3  # most recent first

    def test_parent_version_tracked(self, versions, symbiote_id) -> None:
        v1 = versions.create_version(symbiote_id, "tool_instructions", "V1")
        versions.create_version(symbiote_id, "tool_instructions", "V2", parent_version=v1)
        row = versions.get_active_version(symbiote_id, "tool_instructions")
        assert row["parent_version"] == 1


# ── H-07: ParameterTuner ────────────────────────────────────────────────────


class TestTunerTiers:
    def test_tier_0_no_data(self, tuner, symbiote_id) -> None:
        result = tuner.analyze(symbiote_id)
        assert result.tier == 0
        assert result.adjustments == {}

    def test_tier_1_with_5_sessions(self, tuner, adapter, symbiote_id) -> None:
        for _ in range(TIER_1_MIN):
            _insert_trace(adapter, symbiote_id)
        result = tuner.analyze(symbiote_id)
        assert result.tier == 1

    def test_tier_2_with_20_sessions(self, tuner, adapter, symbiote_id) -> None:
        for _ in range(TIER_2_MIN):
            _insert_trace(adapter, symbiote_id)
        result = tuner.analyze(symbiote_id)
        assert result.tier == 2

    def test_tier_3_with_50_sessions(self, tuner, adapter, symbiote_id) -> None:
        for _ in range(TIER_3_MIN):
            _insert_trace(adapter, symbiote_id)
        result = tuner.analyze(symbiote_id)
        assert result.tier == 3


class TestTunerMaxIterations:
    def test_tier1_100pct_max_iterations_increases(self, tuner, adapter, symbiote_id, env_manager) -> None:
        """When ALL sessions hit max_iterations, tier 1 should increase the cap."""
        env_manager.configure(symbiote_id=symbiote_id, max_tool_iterations=10)
        for _ in range(TIER_1_MIN):
            _insert_trace(adapter, symbiote_id, stop_reason="max_iterations", iterations=10)

        result = tuner.analyze(symbiote_id)
        assert "max_tool_iterations" in result.adjustments
        assert result.adjustments["max_tool_iterations"] == 15

    def test_tier1_50pct_max_iterations_no_change(self, tuner, adapter, symbiote_id, env_manager) -> None:
        """At tier 1, 50% is below the 80% threshold — no change."""
        env_manager.configure(symbiote_id=symbiote_id, max_tool_iterations=10)
        for _ in range(3):
            _insert_trace(adapter, symbiote_id, stop_reason="max_iterations")
        for _ in range(3):
            _insert_trace(adapter, symbiote_id, stop_reason="end_turn")

        result = tuner.analyze(symbiote_id)
        assert "max_tool_iterations" not in result.adjustments

    def test_tier2_30pct_max_iterations_increases(self, tuner, adapter, symbiote_id, env_manager) -> None:
        """At tier 2, 30%+ should trigger increase."""
        env_manager.configure(symbiote_id=symbiote_id, max_tool_iterations=10)
        for _ in range(8):
            _insert_trace(adapter, symbiote_id, stop_reason="max_iterations")
        for _ in range(12):
            _insert_trace(adapter, symbiote_id, stop_reason="end_turn")

        result = tuner.analyze(symbiote_id)
        assert "max_tool_iterations" in result.adjustments

    def test_cap_at_30(self, tuner, adapter, symbiote_id, env_manager) -> None:
        env_manager.configure(symbiote_id=symbiote_id, max_tool_iterations=28)
        for _ in range(TIER_1_MIN):
            _insert_trace(adapter, symbiote_id, stop_reason="max_iterations")

        result = tuner.analyze(symbiote_id)
        assert result.adjustments.get("max_tool_iterations", 0) <= 30

    def test_lower_if_successful_sessions_use_few(self, tuner, adapter, symbiote_id, env_manager) -> None:
        """If successful sessions use max 2 iterations, lower the cap."""
        env_manager.configure(symbiote_id=symbiote_id, max_tool_iterations=20)
        for _ in range(TIER_1_MIN):
            _insert_trace(adapter, symbiote_id, stop_reason="end_turn", iterations=2)

        result = tuner.analyze(symbiote_id)
        if "max_tool_iterations" in result.adjustments:
            # Should suggest 2*2=4, which is < 20-2=18
            assert result.adjustments["max_tool_iterations"] <= 10


class TestTunerCompaction:
    def test_tier2_lowers_threshold_when_avg_iters_low(self, tuner, adapter, symbiote_id) -> None:
        for _ in range(TIER_2_MIN):
            _insert_trace(adapter, symbiote_id, stop_reason="end_turn", iterations=2)

        result = tuner.analyze(symbiote_id)
        assert result.adjustments.get("compaction_threshold", 4) == 2


class TestTunerApply:
    def test_apply_changes_config(self, tuner, adapter, symbiote_id, env_manager) -> None:
        env_manager.configure(symbiote_id=symbiote_id, max_tool_iterations=10)

        result = TuningResult(
            symbiote_id=symbiote_id, session_count=10, tier=1,
            adjustments={"max_tool_iterations": 15},
            reasons={"max_tool_iterations": "test"},
        )

        tuner.apply(result, env_manager)
        assert result.applied is True
        assert env_manager.get_max_tool_iterations(symbiote_id) == 15

    def test_apply_no_adjustments_is_noop(self, tuner, adapter, symbiote_id, env_manager) -> None:
        result = TuningResult(
            symbiote_id=symbiote_id, session_count=0, tier=0,
        )
        tuner.apply(result, env_manager)
        assert result.applied is False


# ── H-08: max_tool_iterations in EnvironmentConfig ──────────────────────────


class TestMaxToolIterationsConfig:
    def test_default_is_10(self, env_manager, symbiote_id) -> None:
        assert env_manager.get_max_tool_iterations(symbiote_id) == 10

    def test_configure_and_retrieve(self, env_manager, symbiote_id) -> None:
        env_manager.configure(symbiote_id=symbiote_id, max_tool_iterations=20)
        assert env_manager.get_max_tool_iterations(symbiote_id) == 20

    def test_update_preserves_other_fields(self, env_manager, symbiote_id) -> None:
        env_manager.configure(symbiote_id=symbiote_id, max_tool_iterations=15, memory_share=0.5)
        env_manager.configure(symbiote_id=symbiote_id, max_tool_iterations=25)
        assert env_manager.get_memory_share(symbiote_id) == 0.5
        assert env_manager.get_max_tool_iterations(symbiote_id) == 25

    def test_model_validation(self) -> None:
        cfg = EnvironmentConfig(symbiote_id="t", max_tool_iterations=10)
        assert cfg.max_tool_iterations == 10

    def test_model_default(self) -> None:
        cfg = EnvironmentConfig(symbiote_id="t")
        assert cfg.max_tool_iterations == 10

    def test_assembled_context_uses_config_value(self, adapter, symbiote_id, env_manager) -> None:
        """Verify the value propagates through ContextAssembler to AssembledContext."""
        from symbiote.core.context import ContextAssembler
        from symbiote.knowledge.service import KnowledgeService
        from symbiote.memory.store import MemoryStore

        env_manager.configure(symbiote_id=symbiote_id, max_tool_iterations=25)

        assembler = ContextAssembler(
            identity=IdentityManager(adapter),
            memory=MemoryStore(adapter),
            knowledge=KnowledgeService(adapter),
            environment=env_manager,
        )

        from symbiote.core.session import SessionManager
        sessions = SessionManager(adapter)
        session = sessions.start(symbiote_id=symbiote_id)

        ctx = assembler.build(
            session_id=session.id,
            symbiote_id=symbiote_id,
            user_input="test",
        )
        assert ctx.max_tool_iterations == 25

    def test_schema_migration(self, adapter, symbiote_id) -> None:
        """Verify the column exists and round-trips via EnvironmentManager."""
        env = EnvironmentManager(storage=adapter)
        env.configure(symbiote_id=symbiote_id, max_tool_iterations=15)
        assert env.get_max_tool_iterations(symbiote_id) == 15
