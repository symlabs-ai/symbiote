"""Tests for H-13: CrossSymbioteLearner — tool overlap and version transfer."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.identity import IdentityManager
from symbiote.environment.manager import EnvironmentManager
from symbiote.harness.cross_learning import CrossSymbioteLearner, LearningTransfer
from symbiote.harness.versions import HarnessVersionRepository

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "cross_learning_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def versions(adapter: SQLiteAdapter) -> HarnessVersionRepository:
    return HarnessVersionRepository(storage=adapter)


@pytest.fixture()
def env(adapter: SQLiteAdapter) -> EnvironmentManager:
    return EnvironmentManager(storage=adapter)


@pytest.fixture()
def learner(adapter: SQLiteAdapter, versions: HarnessVersionRepository) -> CrossSymbioteLearner:
    return CrossSymbioteLearner(storage=adapter, versions=versions)


def _create_symbiote(adapter: SQLiteAdapter, name: str, tools: list[str]) -> str:
    """Create a symbiote with a tool set configured."""
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name=name, role="assistant")
    env = EnvironmentManager(storage=adapter)
    env.configure(symbiote_id=sym.id, tools=tools)
    return sym.id


def _create_version_with_score(
    adapter: SQLiteAdapter,
    versions: HarnessVersionRepository,
    symbiote_id: str,
    component: str,
    content: str,
    avg_score: float,
    session_count: int = 10,
) -> int:
    """Create a harness version and set its score directly."""
    v = versions.create_version(symbiote_id, component, content)
    # Update score directly
    row = versions.get_active_version(symbiote_id, component)
    adapter.execute(
        "UPDATE harness_versions SET avg_score = ?, session_count = ? WHERE id = ?",
        (avg_score, session_count, row["id"]),
    )
    return v


# ── compute_tool_overlap ─────────────────────────────────────────────────────


class TestComputeToolOverlap:
    def test_same_tools_returns_1(self, adapter, learner) -> None:
        a = _create_symbiote(adapter, "A", ["search", "save", "delete"])
        b = _create_symbiote(adapter, "B", ["search", "save", "delete"])

        overlap = learner.compute_tool_overlap(a, b)
        assert overlap == 1.0

    def test_no_overlap_returns_0(self, adapter, learner) -> None:
        a = _create_symbiote(adapter, "A", ["search", "save"])
        b = _create_symbiote(adapter, "B", ["email", "calendar"])

        overlap = learner.compute_tool_overlap(a, b)
        assert overlap == 0.0

    def test_partial_overlap(self, adapter, learner) -> None:
        a = _create_symbiote(adapter, "A", ["search", "save"])
        b = _create_symbiote(adapter, "B", ["search", "email"])

        # Jaccard: intersection={search} / union={search,save,email} = 1/3
        overlap = learner.compute_tool_overlap(a, b)
        assert abs(overlap - 1 / 3) < 0.01

    def test_half_overlap(self, adapter, learner) -> None:
        a = _create_symbiote(adapter, "A", ["search", "save"])
        b = _create_symbiote(adapter, "B", ["search", "save", "email", "calendar"])

        # Jaccard: intersection={search,save} / union={search,save,email,calendar} = 2/4
        overlap = learner.compute_tool_overlap(a, b)
        assert overlap == 0.5

    def test_both_empty_returns_1(self, adapter, learner) -> None:
        a = _create_symbiote(adapter, "A", [])
        b = _create_symbiote(adapter, "B", [])

        overlap = learner.compute_tool_overlap(a, b)
        assert overlap == 1.0

    def test_one_empty_returns_0(self, adapter, learner) -> None:
        a = _create_symbiote(adapter, "A", ["search"])
        b = _create_symbiote(adapter, "B", [])

        overlap = learner.compute_tool_overlap(a, b)
        assert overlap == 0.0


# ── find_candidates ──────────────────────────────────────────────────────────


class TestFindCandidates:
    def test_good_version_high_overlap_found(self, adapter, versions, learner) -> None:
        source = _create_symbiote(adapter, "Source", ["search", "save"])
        target = _create_symbiote(adapter, "Target", ["search", "save"])

        _create_version_with_score(
            adapter, versions, source, "tool_instructions",
            "Be concise when using tools.", avg_score=0.85,
        )

        candidates = learner.find_candidates(target, min_overlap=0.5)

        assert len(candidates) == 1
        assert candidates[0].source_symbiote == source
        assert candidates[0].target_symbiote == target
        assert candidates[0].component == "tool_instructions"
        assert candidates[0].source_avg_score == 0.85
        assert candidates[0].tool_overlap == 1.0

    def test_low_overlap_not_found(self, adapter, versions, learner) -> None:
        source = _create_symbiote(adapter, "Source", ["email", "calendar"])
        target = _create_symbiote(adapter, "Target", ["search", "save"])

        _create_version_with_score(
            adapter, versions, source, "tool_instructions",
            "Be concise.", avg_score=0.9,
        )

        candidates = learner.find_candidates(target, min_overlap=0.5)
        assert len(candidates) == 0

    def test_low_score_not_found(self, adapter, versions, learner) -> None:
        source = _create_symbiote(adapter, "Source", ["search", "save"])
        target = _create_symbiote(adapter, "Target", ["search", "save"])

        _create_version_with_score(
            adapter, versions, source, "tool_instructions",
            "Bad instructions.", avg_score=0.3,
        )

        candidates = learner.find_candidates(target, min_overlap=0.5)
        assert len(candidates) == 0

    def test_target_already_has_component_not_found(self, adapter, versions, learner) -> None:
        source = _create_symbiote(adapter, "Source", ["search", "save"])
        target = _create_symbiote(adapter, "Target", ["search", "save"])

        _create_version_with_score(
            adapter, versions, source, "tool_instructions",
            "Source instructions.", avg_score=0.85,
        )
        # Target already has its own version
        versions.create_version(target, "tool_instructions", "Target instructions.")

        candidates = learner.find_candidates(target, min_overlap=0.5)
        assert len(candidates) == 0


# ── transfer ──────────────────────────────────────────────────────────────────


class TestTransfer:
    def test_transfer_creates_version_on_target(self, adapter, versions, learner) -> None:
        source = _create_symbiote(adapter, "Source", ["search"])
        target = _create_symbiote(adapter, "Target", ["search"])

        transfer = LearningTransfer(
            source_symbiote=source,
            target_symbiote=target,
            component="tool_instructions",
            source_version=1,
            content="Transferred instructions.",
            source_avg_score=0.9,
            tool_overlap=1.0,
        )

        new_version = learner.transfer(transfer)

        assert new_version == 1
        # Verify the version exists on the target
        content = versions.get_active(target, "tool_instructions")
        assert content == "Transferred instructions."

    def test_transfer_does_not_affect_source(self, adapter, versions, learner) -> None:
        source = _create_symbiote(adapter, "Source", ["search"])
        target = _create_symbiote(adapter, "Target", ["search"])

        # Source has its own version
        _create_version_with_score(
            adapter, versions, source, "tool_instructions",
            "Source content.", avg_score=0.9,
        )

        transfer = LearningTransfer(
            source_symbiote=source,
            target_symbiote=target,
            component="tool_instructions",
            source_version=1,
            content="Source content.",
            source_avg_score=0.9,
            tool_overlap=1.0,
        )

        learner.transfer(transfer)

        # Source still has its original version
        assert versions.get_active(source, "tool_instructions") == "Source content."
        # Target has the transferred version
        assert versions.get_active(target, "tool_instructions") == "Source content."
