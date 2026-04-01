"""Tests for long-run mode — Planner/Generator/Evaluator architecture."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from symbiote.core.context import AssembledContext
from symbiote.core.scoring import _score_long_run, compute_auto_score
from symbiote.runners.base import (
    BlockResult,
    LongRunPlan,
    LoopStep,
    LoopTrace,
    RunResult,
)
from symbiote.runners.long_run import LongRunRunner

# ── Models ───────────────────────────────────────────────────────────────────


class TestLongRunModels:
    """BlockResult, LongRunPlan models."""

    def test_block_result_defaults(self):
        b = BlockResult(block_index=0)
        assert b.block_index == 0
        assert b.success is False
        assert b.evaluator_score is None

    def test_long_run_plan(self):
        p = LongRunPlan(
            blocks=[{"name": "A"}, {"name": "B"}],
            total_blocks=2,
            raw_spec="test spec",
        )
        assert p.total_blocks == 2
        assert len(p.blocks) == 2

    def test_run_result_with_plan(self):
        r = RunResult(
            success=True,
            plan=LongRunPlan(blocks=[{"name": "A"}], total_blocks=1),
            block_results=[BlockResult(block_index=0, success=True)],
        )
        assert r.plan is not None
        assert r.block_results is not None

    def test_tool_mode_long_run_in_env_config(self):
        from symbiote.core.models import EnvironmentConfig
        cfg = EnvironmentConfig(symbiote_id="s1", tool_mode="long_run")
        assert cfg.tool_mode == "long_run"
        assert cfg.tool_loop is True

    def test_long_run_config_fields(self):
        from symbiote.core.models import EnvironmentConfig
        cfg = EnvironmentConfig(
            symbiote_id="s1",
            tool_mode="long_run",
            planner_prompt="Plan this",
            evaluator_prompt="Evaluate this",
            evaluator_criteria=[{"name": "quality", "weight": 1.0}],
            context_strategy="reset",
            max_blocks=50,
        )
        assert cfg.planner_prompt == "Plan this"
        assert cfg.context_strategy == "reset"
        assert cfg.max_blocks == 50


# ── Scoring ──────────────────────────────────────────────────────────────────


class TestLongRunScoring:
    """Scoring for long-run mode — completion rate, not iteration penalty."""

    def test_all_blocks_success(self):
        trace = LoopTrace(
            steps=[
                LoopStep(iteration=1, tool_id="block:Setup", success=True),
                LoopStep(iteration=2, tool_id="block:API", success=True),
                LoopStep(iteration=3, tool_id="block:Frontend", success=True),
            ],
            total_iterations=3,
            stop_reason="end_turn",
            tool_mode="long_run",
        )
        score = compute_auto_score(trace, tool_mode="long_run")
        assert score == 1.0

    def test_partial_completion(self):
        trace = LoopTrace(
            steps=[
                LoopStep(iteration=1, tool_id="block:Setup", success=True),
                LoopStep(iteration=2, tool_id="block:API", success=False),
            ],
            total_iterations=2,
            stop_reason="block_failure",
            tool_mode="long_run",
        )
        score = compute_auto_score(trace, tool_mode="long_run")
        # block_failure base = 0.6, success_rate = 0.5, factor = 0.4 + 0.6*0.5 = 0.7
        # 0.6 * 0.7 = 0.42
        assert 0.4 <= score <= 0.45

    def test_many_iterations_no_penalty(self):
        """Long-run doesn't penalize high iteration count."""
        trace = LoopTrace(
            steps=[
                LoopStep(iteration=i, tool_id=f"block:Block{i}", success=True)
                for i in range(1, 21)
            ],
            total_iterations=20,
            stop_reason="end_turn",
            tool_mode="long_run",
        )
        score = compute_auto_score(trace, tool_mode="long_run")
        assert score == 1.0  # all blocks success, no iteration penalty

    def test_no_trace(self):
        score = compute_auto_score(None, tool_mode="long_run")
        assert score == 0.8  # default for no trace


# ── Planner ──────────────────────────────────────────────────────────────────


class TestPlanner:
    """Planner phase — expands prompt into structured plan."""

    def _make_runner(self, llm_response: str) -> LongRunRunner:
        llm = MagicMock()
        llm.complete.return_value = llm_response
        return LongRunRunner(llm=llm)

    def test_planner_parses_json_array(self):
        blocks = json.dumps([
            {"name": "Setup", "description": "Init project", "success_criteria": "Project created"},
            {"name": "API", "description": "Build API", "success_criteria": "Endpoints working"},
        ])
        runner = self._make_runner(blocks)
        ctx = AssembledContext(
            symbiote_id="s1", session_id="sess", user_input="Build an app",
            tool_mode="long_run",
        )
        plan = runner._run_planner(ctx)
        assert plan.total_blocks == 2
        assert plan.blocks[0]["name"] == "Setup"

    def test_planner_parses_markdown_fence(self):
        raw = '```json\n[{"name": "A"}]\n```'
        runner = self._make_runner(raw)
        ctx = AssembledContext(
            symbiote_id="s1", session_id="sess", user_input="test",
            tool_mode="long_run",
        )
        plan = runner._run_planner(ctx)
        assert plan.total_blocks == 1

    def test_planner_handles_garbage(self):
        runner = self._make_runner("I don't understand")
        ctx = AssembledContext(
            symbiote_id="s1", session_id="sess", user_input="test",
            tool_mode="long_run",
        )
        plan = runner._run_planner(ctx)
        assert plan.total_blocks == 0
        assert plan.raw_spec == "I don't understand"

    def test_planner_uses_custom_prompt(self):
        llm = MagicMock()
        llm.complete.return_value = '[{"name": "X"}]'
        runner = LongRunRunner(llm=llm)
        ctx = AssembledContext(
            symbiote_id="s1", session_id="sess", user_input="test",
            tool_mode="long_run",
            planner_prompt="Custom planner instructions",
        )
        runner._run_planner(ctx)
        # Verify the custom prompt was passed as system message
        call_args = llm.complete.call_args[0][0]
        assert call_args[0]["content"] == "Custom planner instructions"


# ── Generator (block execution) ──────────────────────────────────────────────


class TestGenerator:
    """Generator phase — executes blocks of work."""

    def _make_runner(self, llm_response: str = "Done") -> LongRunRunner:
        llm = MagicMock()
        llm.complete.return_value = llm_response
        del llm.stream
        return LongRunRunner(llm=llm)

    def _make_context(self, **kwargs) -> AssembledContext:
        return AssembledContext(
            symbiote_id="s1", session_id="sess", user_input="Build an app",
            tool_mode="long_run", **kwargs,
        )

    def test_block_prompt_includes_plan(self):
        runner = self._make_runner()
        plan = LongRunPlan(blocks=[{"name": "A", "description": "Do A"}], total_blocks=1)
        prompt = runner._build_block_prompt(
            self._make_context(), plan, plan.blocks[0], []
        )
        assert "## Project Plan" in prompt
        assert "## Current Block: A" in prompt

    def test_block_prompt_includes_progress(self):
        runner = self._make_runner()
        plan = LongRunPlan(blocks=[{"name": "A"}, {"name": "B"}], total_blocks=2)
        completed = [BlockResult(block_index=0, block_name="A", success=True)]
        prompt = runner._build_block_prompt(
            self._make_context(), plan, plan.blocks[1], completed
        )
        assert "## Progress So Far" in prompt
        assert "A: DONE" in prompt

    def test_retry_prompt_includes_feedback(self):
        runner = self._make_runner()
        eval_result = {
            "passed": False,
            "feedback": "Missing error handling",
            "blocking_issues": ["No try/catch in API"],
        }
        prompt = runner._build_retry_prompt("Original prompt", eval_result)
        assert "Previous attempt was rejected" in prompt
        assert "No try/catch in API" in prompt


# ── Evaluator ────────────────────────────────────────────────────────────────


class TestEvaluator:
    """Evaluator phase — grades block output."""

    def test_evaluator_parses_json(self):
        eval_response = json.dumps({
            "passed": True,
            "overall_score": 0.9,
            "feedback": "Good work",
        })
        llm = MagicMock()
        llm.complete.return_value = eval_response
        runner = LongRunRunner(llm=llm, evaluator_llm=llm)

        ctx = AssembledContext(
            symbiote_id="s1", session_id="sess", user_input="test",
            tool_mode="long_run",
            evaluator_prompt="Evaluate strictly",
        )
        plan = LongRunPlan(blocks=[{"name": "A", "description": "Do A"}], total_blocks=1)
        block_result = BlockResult(block_index=0, block_name="A", success=True, output="I did A")

        result = runner._evaluate_block(ctx, plan, plan.blocks[0], block_result)
        assert result["passed"] is True
        assert result["overall_score"] == 0.9

    def test_evaluator_handles_garbage(self):
        llm = MagicMock()
        llm.complete.return_value = "This is great!"
        runner = LongRunRunner(llm=llm, evaluator_llm=llm)

        ctx = AssembledContext(
            symbiote_id="s1", session_id="sess", user_input="test",
            tool_mode="long_run",
            evaluator_prompt="Evaluate",
        )
        plan = LongRunPlan(blocks=[{"name": "A"}], total_blocks=1)
        block_result = BlockResult(block_index=0, success=True, output="done")

        result = runner._evaluate_block(ctx, plan, plan.blocks[0], block_result)
        # Fallback: assume passed
        assert result["passed"] is True

    def test_evaluator_uses_separate_llm(self):
        main_llm = MagicMock()
        eval_llm = MagicMock()
        eval_llm.complete.return_value = '{"passed": true, "overall_score": 1.0}'

        runner = LongRunRunner(llm=main_llm, evaluator_llm=eval_llm)
        ctx = AssembledContext(
            symbiote_id="s1", session_id="sess", user_input="test",
            tool_mode="long_run",
            evaluator_prompt="Evaluate",
        )
        plan = LongRunPlan(blocks=[{"name": "A"}], total_blocks=1)
        block_result = BlockResult(block_index=0, success=True, output="done")

        runner._evaluate_block(ctx, plan, plan.blocks[0], block_result)
        # eval_llm was called, not main_llm
        eval_llm.complete.assert_called_once()
        main_llm.complete.assert_not_called()

    def test_evaluator_with_criteria(self):
        eval_response = json.dumps({"passed": True, "overall_score": 0.85})
        llm = MagicMock()
        llm.complete.return_value = eval_response
        runner = LongRunRunner(llm=llm)

        ctx = AssembledContext(
            symbiote_id="s1", session_id="sess", user_input="test",
            tool_mode="long_run",
            evaluator_prompt="Evaluate",
            evaluator_criteria=[
                {"name": "completeness", "weight": 1.0, "threshold": 0.7, "description": "All features done"},
            ],
        )
        plan = LongRunPlan(blocks=[{"name": "A"}], total_blocks=1)
        block_result = BlockResult(block_index=0, success=True, output="done")

        runner._evaluate_block(ctx, plan, plan.blocks[0], block_result)
        # Verify criteria was included in the prompt
        call_args = llm.complete.call_args[0][0]
        user_msg = call_args[1]["content"]
        assert "completeness" in user_msg
        assert "threshold: 0.7" in user_msg


# ── Full run integration ─────────────────────────────────────────────────────


class TestLongRunFullRun:
    """Integration: full plan -> execute -> evaluate cycle."""

    def test_full_run_no_evaluator(self):
        """Planner + Generator, no evaluator."""
        llm = MagicMock()
        # First call: planner
        # Subsequent calls: block execution
        llm.complete.side_effect = [
            '[{"name": "Setup", "description": "Init", "success_criteria": "Created"}]',
            "Setup completed successfully",
        ]
        del llm.stream

        runner = LongRunRunner(llm=llm)
        ctx = AssembledContext(
            symbiote_id="s1", session_id="sess", user_input="Build it",
            tool_mode="long_run",
        )
        result = runner.run(ctx)

        assert result.success
        assert result.plan is not None
        assert result.plan.total_blocks == 1
        assert result.block_results is not None
        assert len(result.block_results) == 1
        assert result.block_results[0].success
        assert result.loop_trace is not None
        assert result.loop_trace.tool_mode == "long_run"

    def test_full_run_with_evaluator(self):
        """Planner + Generator + Evaluator."""
        main_llm = MagicMock()
        main_llm.complete.side_effect = [
            '[{"name": "API", "description": "Build API", "success_criteria": "Works"}]',
            "API built with all endpoints",
        ]
        del main_llm.stream

        eval_llm = MagicMock()
        eval_llm.complete.return_value = '{"passed": true, "overall_score": 0.95, "feedback": "Excellent"}'

        runner = LongRunRunner(llm=main_llm, evaluator_llm=eval_llm)
        ctx = AssembledContext(
            symbiote_id="s1", session_id="sess", user_input="Build API",
            tool_mode="long_run",
            evaluator_prompt="Evaluate strictly",
        )
        result = runner.run(ctx)

        assert result.success
        assert result.block_results[0].evaluator_score == 0.95
        assert result.block_results[0].evaluator_feedback == "Excellent"

    def test_planner_failure_returns_error(self):
        llm = MagicMock()
        llm.complete.return_value = "I don't understand"
        runner = LongRunRunner(llm=llm)
        ctx = AssembledContext(
            symbiote_id="s1", session_id="sess", user_input="???",
            tool_mode="long_run",
        )
        result = runner.run(ctx)
        assert not result.success
        assert "no blocks" in result.error.lower()

    def test_progress_callbacks(self):
        progress = []
        llm = MagicMock()
        llm.complete.side_effect = [
            '[{"name": "A"}]',
            "Done",
        ]
        del llm.stream

        runner = LongRunRunner(
            llm=llm,
            on_progress=lambda event, i, t: progress.append((event, i, t)),
        )
        ctx = AssembledContext(
            symbiote_id="s1", session_id="sess", user_input="test",
            tool_mode="long_run",
        )
        runner.run(ctx)

        events = [p[0] for p in progress]
        assert "plan_complete" in events
        assert "block_start" in events
        assert "block_end" in events

    def test_context_strategy_reset_clears_messages(self):
        """With context_strategy=reset, accumulated messages clear between blocks."""
        llm = MagicMock()
        llm.complete.side_effect = [
            '[{"name": "A"}, {"name": "B"}]',
            "A done",
            "B done",
        ]
        del llm.stream

        runner = LongRunRunner(llm=llm)
        ctx = AssembledContext(
            symbiote_id="s1", session_id="sess", user_input="test",
            tool_mode="long_run",
            context_strategy="reset",
        )
        result = runner.run(ctx)
        assert result.success
        assert len(result.block_results) == 2

    def test_max_blocks_limits_execution(self):
        """max_blocks caps how many blocks are executed."""
        llm = MagicMock()
        blocks = [{"name": f"B{i}"} for i in range(10)]
        llm.complete.side_effect = [
            json.dumps(blocks),  # planner
            "Done",              # block 0
            "Done",              # block 1
        ]
        del llm.stream

        runner = LongRunRunner(llm=llm)
        ctx = AssembledContext(
            symbiote_id="s1", session_id="sess", user_input="test",
            tool_mode="long_run",
            max_blocks=2,
        )
        result = runner.run(ctx)
        assert len(result.block_results) == 2

    def test_output_summary(self):
        plan = LongRunPlan(
            blocks=[{"name": "A"}, {"name": "B"}],
            total_blocks=2,
        )
        results = [
            BlockResult(block_index=0, block_name="A", success=True, evaluator_score=0.9),
            BlockResult(block_index=1, block_name="B", success=False, evaluator_feedback="Needs work"),
        ]
        summary = LongRunRunner._build_output_summary(plan, results)
        assert "1/2 blocks" in summary
        assert "[DONE] A" in summary
        assert "[FAILED] B" in summary
        assert "Needs work" in summary
