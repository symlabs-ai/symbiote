"""Tests for H-12: StructuralEvolver — pluggable strategy registry."""

from __future__ import annotations

import pytest

from symbiote.harness.structural import StructuralEvolver, StructuralProposal

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_proposal(**overrides) -> StructuralProposal:
    defaults = {
        "id": "prop-1",
        "component": "context_assembler",
        "change_type": "parameter",
        "description": "max_tool_iterations",
        "current_value": 10,
        "proposed_value": 15,
        "confidence": 0.8,
        "evidence": "test evidence",
    }
    defaults.update(overrides)
    return StructuralProposal(**defaults)


class FakeEnvManager:
    """Mock EnvironmentManager that records configure calls."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def configure(self, **kwargs) -> None:
        self.calls.append(kwargs)


class BrokenEnvManager:
    """Mock EnvironmentManager that raises on configure."""

    def configure(self, **kwargs) -> None:
        raise RuntimeError("configure failed")


# ── Register + Propose ────────────────────────────────────────────────────────


class TestRegisterAndPropose:
    def test_register_strategy_and_propose(self) -> None:
        evolver = StructuralEvolver()

        def strategy(storage, symbiote_id):
            return [_make_proposal(id="s1")]

        evolver.register_strategy(strategy)
        proposals = evolver.propose(storage=None, symbiote_id="sym-1")

        assert len(proposals) == 1
        assert proposals[0].id == "s1"

    def test_multiple_strategies(self) -> None:
        evolver = StructuralEvolver()

        def strategy_a(storage, symbiote_id):
            return [_make_proposal(id="a1")]

        def strategy_b(storage, symbiote_id):
            return [_make_proposal(id="b1"), _make_proposal(id="b2")]

        evolver.register_strategy(strategy_a)
        evolver.register_strategy(strategy_b)
        proposals = evolver.propose(storage=None, symbiote_id="sym-1")

        assert len(proposals) == 3
        ids = {p.id for p in proposals}
        assert ids == {"a1", "b1", "b2"}

    def test_empty_strategy_returns_empty(self) -> None:
        evolver = StructuralEvolver()

        def noop(storage, symbiote_id):
            return []

        evolver.register_strategy(noop)
        proposals = evolver.propose(storage=None, symbiote_id="sym-1")
        assert proposals == []


# ── Strategy error handling ───────────────────────────────────────────────────


class TestStrategyErrorHandling:
    def test_failing_strategy_caught_others_still_run(self) -> None:
        evolver = StructuralEvolver()

        def bad_strategy(storage, symbiote_id):
            raise ValueError("boom")

        def good_strategy(storage, symbiote_id):
            return [_make_proposal(id="good")]

        evolver.register_strategy(bad_strategy)
        evolver.register_strategy(good_strategy)
        proposals = evolver.propose(storage=None, symbiote_id="sym-1")

        assert len(proposals) == 1
        assert proposals[0].id == "good"


# ── Apply ─────────────────────────────────────────────────────────────────────


class TestApply:
    def test_apply_parameter_proposal(self) -> None:
        evolver = StructuralEvolver()
        env = FakeEnvManager()
        proposal = _make_proposal(
            component="sym-1",
            change_type="parameter",
            description="max_tool_iterations",
            proposed_value=15,
        )

        ok = evolver.apply(proposal, env)

        assert ok is True
        assert len(env.calls) == 1
        assert env.calls[0]["symbiote_id"] == "sym-1"
        assert env.calls[0]["max_tool_iterations"] == 15

    def test_apply_non_parameter_returns_false(self) -> None:
        evolver = StructuralEvolver()
        env = FakeEnvManager()
        proposal = _make_proposal(change_type="strategy")

        ok = evolver.apply(proposal, env)

        assert ok is False
        assert len(env.calls) == 0

    def test_apply_pipeline_returns_false(self) -> None:
        evolver = StructuralEvolver()
        env = FakeEnvManager()
        proposal = _make_proposal(change_type="pipeline")

        ok = evolver.apply(proposal, env)
        assert ok is False

    def test_apply_no_configure_method_returns_false(self) -> None:
        evolver = StructuralEvolver()
        proposal = _make_proposal(change_type="parameter")

        ok = evolver.apply(proposal, object())  # no configure method
        assert ok is False

    def test_apply_configure_raises_returns_false(self) -> None:
        evolver = StructuralEvolver()
        env = BrokenEnvManager()
        proposal = _make_proposal(change_type="parameter")

        ok = evolver.apply(proposal, env)
        assert ok is False
