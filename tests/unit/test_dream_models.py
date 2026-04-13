"""Tests for Dream Mode models."""

from __future__ import annotations

from symbiote.dream.models import BudgetTracker, DreamPhaseResult, DreamReport


class TestBudgetTracker:
    def test_consume_within_limit(self):
        b = BudgetTracker(5)
        assert b.consume(3) is True
        assert b.used == 3
        assert b.remaining == 2

    def test_consume_exact_limit(self):
        b = BudgetTracker(3)
        assert b.consume(3) is True
        assert b.remaining == 0

    def test_consume_over_limit_rejected(self):
        b = BudgetTracker(3)
        b.consume(2)
        assert b.consume(2) is False
        assert b.used == 2  # unchanged

    def test_remaining_never_negative(self):
        b = BudgetTracker(0)
        assert b.remaining == 0
        assert b.consume(1) is False


class TestDreamReport:
    def test_serialization(self):
        report = DreamReport(symbiote_id="sym-1", dream_mode="light")
        data = report.model_dump(mode="json")
        assert data["symbiote_id"] == "sym-1"
        assert data["dream_mode"] == "light"
        assert data["dry_run"] is False
        assert data["phases"] == []

    def test_with_phases(self):
        phase = DreamPhaseResult(phase="prune", actions_proposed=3, actions_applied=2)
        report = DreamReport(
            symbiote_id="sym-1",
            dream_mode="full",
            phases=[phase],
            total_llm_calls=5,
        )
        assert len(report.phases) == 1
        assert report.phases[0].actions_proposed == 3
        assert report.total_llm_calls == 5
