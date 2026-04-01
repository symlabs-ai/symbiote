"""Tests for brief mode — sync trace, calibrated scoring, multi-step instructions."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from symbiote.core.context import AssembledContext
from symbiote.core.scoring import compute_auto_score
from symbiote.runners.base import LoopStep, LoopTrace, RunResult
from symbiote.runners.chat import _TOOL_INSTRUCTIONS, ChatRunner

# ── Scoring calibration ─────────────────────────────────────────────────────


class TestBriefScoringCalibration:
    """Brief mode scoring is calibrated for multi-step tasks (3-10 iterations normal)."""

    @pytest.mark.parametrize(
        "iters, expected",
        [
            (1, 1.0),    # single-shot within brief = perfect
            (2, 1.0),    # 2 iterations = still perfect
            (3, 1.0),    # 3 iterations = no penalty (multi-step normal)
            (5, 0.85),   # 5 iterations = slight penalty
            (7, 0.85),   # 7 iterations = still moderate
            (10, 0.7),   # 10 iterations = moderate penalty
            (15, 0.5),   # 15 iterations = significant penalty
        ],
    )
    def test_iteration_penalty_curve(self, iters: int, expected: float):
        """Brief iteration penalty curve is generous for 3-10 iterations."""
        trace = LoopTrace(
            steps=[LoopStep(iteration=i, tool_id="t", success=True) for i in range(1, iters + 1)],
            total_iterations=iters,
            total_tool_calls=iters,
            stop_reason="end_turn",
            tool_mode="brief",
        )
        assert compute_auto_score(trace, tool_mode="brief") == expected

    def test_stagnation_still_penalized(self):
        """Stagnation in brief mode is still heavily penalized."""
        trace = LoopTrace(
            steps=[LoopStep(iteration=1, tool_id="t", success=True)],
            total_iterations=5,
            total_tool_calls=5,
            stop_reason="stagnation",
            tool_mode="brief",
        )
        assert compute_auto_score(trace, tool_mode="brief") == 0.2

    def test_circuit_breaker_still_penalized(self):
        """Circuit breaker in brief mode is still heavily penalized."""
        trace = LoopTrace(
            steps=[LoopStep(iteration=1, tool_id="t", success=False)],
            total_iterations=3,
            total_tool_calls=3,
            stop_reason="circuit_breaker",
            tool_mode="brief",
        )
        score = compute_auto_score(trace, tool_mode="brief")
        # 0.1 base * (1 - 1.0 * 0.3) = 0.07 — failure rate compounds
        assert score < 0.15

    def test_failure_rate_compounds_with_iteration_penalty(self):
        """Brief: tool failures reduce score on top of iteration penalty."""
        # 6 iters, 2 failures = 0.85 * (1 - 2/6 * 0.3) = 0.85 * 0.9 = 0.765
        trace = LoopTrace(
            steps=[
                LoopStep(iteration=1, tool_id="t", success=True),
                LoopStep(iteration=2, tool_id="t", success=False),
                LoopStep(iteration=3, tool_id="t", success=True),
                LoopStep(iteration=4, tool_id="t", success=False),
                LoopStep(iteration=5, tool_id="t", success=True),
                LoopStep(iteration=6, tool_id="t", success=True),
            ],
            total_iterations=6,
            total_tool_calls=6,
            stop_reason="end_turn",
            tool_mode="brief",
        )
        score = compute_auto_score(trace, tool_mode="brief")
        # 0.85 * (1 - 2/6 * 0.3) = 0.85 * 0.9 = 0.765 → rounds to 0.77
        assert 0.75 <= score <= 0.80

    def test_brief_vs_continuous_different_penalties(self):
        """Same trace scores differently in brief vs continuous."""
        trace = LoopTrace(
            steps=[LoopStep(iteration=i, tool_id="t", success=True) for i in range(1, 9)],
            total_iterations=8,
            total_tool_calls=8,
            stop_reason="end_turn",
        )
        brief_score = compute_auto_score(trace, tool_mode="brief")
        continuous_score = compute_auto_score(trace, tool_mode="continuous")
        # 8 iters: brief = 0.7 (8 <= 10), continuous = 0.8 (8 <= 10)
        assert continuous_score > brief_score


# ── Sync trace generation ───────────────────────────────────────────────────


class TestBriefSyncTrace:
    """_run_loop sync generates LoopTrace like run_async does."""

    def _make_context(self, tool_mode: str = "brief") -> AssembledContext:
        return AssembledContext(
            symbiote_id="test-sym",
            session_id="test-sess",
            user_input="hello",
            tool_mode=tool_mode,
            tool_loop=True,
        )

    def _make_runner(self, llm_response: str = "ok") -> ChatRunner:
        llm = MagicMock()
        llm.complete.return_value = llm_response
        del llm.stream
        return ChatRunner(llm=llm)

    def test_sync_no_tools_returns_no_trace(self):
        """Brief sync without tool calls returns no trace (no steps to record)."""
        runner = self._make_runner(llm_response="direct answer")
        ctx = self._make_context()
        result = runner.run(ctx)
        assert result.success
        # No tool calls = no steps = trace is None
        assert result.loop_trace is None

    def test_sync_with_tools_returns_trace(self):
        """Brief sync with tool calls returns LoopTrace with steps."""
        llm = MagicMock()
        # First call returns tool call, second returns final text
        llm.complete.side_effect = [
            '```tool_call\n{"tool": "search", "params": {"q": "test"}}\n```',
            "Found results",
        ]
        del llm.stream

        gw = MagicMock()
        from symbiote.environment.descriptors import ToolCallResult
        gw.execute_tool_calls.return_value = [
            ToolCallResult(tool_id="search", success=True, output={"items": []})
        ]

        runner = ChatRunner(llm=llm, tool_gateway=gw)
        ctx = self._make_context()
        result = runner.run(ctx)

        assert result.success
        assert result.loop_trace is not None
        assert result.loop_trace.tool_mode == "brief"
        assert result.loop_trace.stop_reason == "end_turn"
        assert result.loop_trace.total_iterations >= 1
        assert len(result.loop_trace.steps) >= 1
        assert result.loop_trace.steps[0].tool_id == "search"
        assert result.loop_trace.steps[0].success is True

    def test_sync_trace_records_elapsed_ms(self):
        """Brief sync trace records elapsed time."""
        llm = MagicMock()
        llm.complete.side_effect = [
            '```tool_call\n{"tool": "ping", "params": {}}\n```',
            "pong",
        ]
        del llm.stream

        gw = MagicMock()
        from symbiote.environment.descriptors import ToolCallResult
        gw.execute_tool_calls.return_value = [
            ToolCallResult(tool_id="ping", success=True, output="ok")
        ]

        runner = ChatRunner(llm=llm, tool_gateway=gw)
        ctx = self._make_context()
        result = runner.run(ctx)

        assert result.loop_trace is not None
        assert result.loop_trace.total_elapsed_ms >= 0

    def test_sync_trace_on_error(self):
        """Brief sync returns partial trace when LLM fails."""
        llm = MagicMock()
        llm.complete.side_effect = ConnectionError("timeout")
        del llm.stream

        runner = ChatRunner(llm=llm)
        ctx = self._make_context()
        result = runner.run(ctx)

        assert not result.success
        assert result.loop_trace is not None
        assert result.loop_trace.tool_mode == "brief"


# ── Tool instructions multi-step ────────────────────────────────────────────


class TestBriefToolInstructions:
    """Tool instructions include multi-step continuation guidance."""

    def test_multi_step_continuation_in_instructions(self):
        """_TOOL_INSTRUCTIONS contains multi-step continuation rule."""
        assert "FULL request has been satisfied" in _TOOL_INSTRUCTIONS
        assert "multiple steps" in _TOOL_INSTRUCTIONS
        assert "continue executing the remaining steps" in _TOOL_INSTRUCTIONS

    def test_instructions_still_have_critical_rules(self):
        """Existing CRITICAL rules are preserved."""
        assert "Match intent to action" in _TOOL_INSTRUCTIONS
        assert "JUDGE whether it actually" in _TOOL_INSTRUCTIONS
        assert "STOP your response immediately" in _TOOL_INSTRUCTIONS

    def test_instructions_evolvable(self):
        """Tool instructions can be overridden per-mode via harness_versions."""
        runner = ChatRunner.__new__(ChatRunner)
        runner._native_tools = False
        ctx = AssembledContext(
            symbiote_id="s1",
            session_id="sess",
            user_input="test",
            tool_mode="brief",
            tool_instructions_override="Custom brief instructions",
            available_tools=[{"tool_id": "t1", "name": "Test", "description": "test"}],
        )
        system = runner._build_system(ctx)
        assert "Custom brief instructions" in system
        assert "FULL request has been satisfied" not in system  # override replaces default
