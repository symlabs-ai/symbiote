"""BenchmarkRunner — evaluate a symbiote against predefined tasks.

Runs benchmark tasks, grades them automatically (tool_called, param_match,
or custom grader), and produces aggregate results.  Designed for CI,
regression testing, and harness evolution validation.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class BenchmarkTask:
    """A single benchmark task with automated grading."""

    id: str
    description: str  # What to ask the symbiote
    expected_tools: list[str] = field(default_factory=list)
    expected_params: dict[str, Any] = field(default_factory=dict)
    grading: Literal["tool_called", "param_match", "custom"] = "tool_called"
    custom_grader: Callable[..., float] | None = None
    timeout: float = 60.0


@dataclass
class BenchmarkResult:
    """Result of running one benchmark task."""

    task_id: str
    score: float  # 0.0-1.0
    passed: bool
    iterations: int
    tool_calls_made: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    error: str | None = None


@dataclass
class BenchmarkSuiteResult:
    """Aggregate results from running a full suite."""

    suite_name: str
    symbiote_id: str
    total_tasks: int
    passed: int
    failed: int
    avg_score: float
    results: list[BenchmarkResult] = field(default_factory=list)
    elapsed_ms: int = 0


# ── Runner ────────────────────────────────────────────────────────────────────


class BenchmarkRunner:
    """Runs benchmark tasks against a symbiote and grades results."""

    def __init__(self, kernel: object) -> None:  # SymbioteKernel
        self._kernel = kernel

    def run_task(self, symbiote_id: str, task: BenchmarkTask) -> BenchmarkResult:
        """Run a single benchmark task and grade it."""
        start = time.monotonic()
        try:
            # 1. Start session
            session = self._kernel.start_session(symbiote_id, goal=task.description)

            # 2. Send task description as message
            self._kernel.message(session.id, task.description)

            # 3. Collect trace
            trace = getattr(self._kernel, "_last_trace", None)
            tool_calls_made: list[str] = []
            iterations = 0

            if trace is not None:
                iterations = trace.total_iterations
                tool_calls_made = [step.tool_id for step in trace.steps]

            # 4. Grade
            score = self._grade(task, trace)

            # 5. Close session
            self._kernel.close_session(session.id)

            elapsed = int((time.monotonic() - start) * 1000)
            return BenchmarkResult(
                task_id=task.id,
                score=score,
                passed=score >= 0.5,
                iterations=iterations,
                tool_calls_made=tool_calls_made,
                elapsed_ms=elapsed,
            )
        except Exception as exc:
            elapsed = int((time.monotonic() - start) * 1000)
            logger.warning("[benchmark] task %s failed: %s", task.id, exc)
            return BenchmarkResult(
                task_id=task.id,
                score=0.0,
                passed=False,
                iterations=0,
                elapsed_ms=elapsed,
                error=str(exc),
            )

    def run_suite(
        self,
        symbiote_id: str,
        tasks: list[BenchmarkTask],
        suite_name: str = "default",
    ) -> BenchmarkSuiteResult:
        """Run all tasks and return aggregate results."""
        start = time.monotonic()
        results: list[BenchmarkResult] = []

        for task in tasks:
            result = self.run_task(symbiote_id, task)
            results.append(result)

        elapsed = int((time.monotonic() - start) * 1000)
        passed = sum(1 for r in results if r.passed)
        scores = [r.score for r in results]
        avg_score = sum(scores) / len(scores) if scores else 0.0

        return BenchmarkSuiteResult(
            suite_name=suite_name,
            symbiote_id=symbiote_id,
            total_tasks=len(results),
            passed=passed,
            failed=len(results) - passed,
            avg_score=round(avg_score, 4),
            results=results,
            elapsed_ms=elapsed,
        )

    # ── Grading ───────────────────────────────────────────────────────────

    def _grade(self, task: BenchmarkTask, trace: object | None) -> float:
        """Dispatch grading based on task.grading strategy."""
        if task.grading == "custom" and task.custom_grader is not None:
            try:
                return task.custom_grader(trace)
            except Exception as exc:
                logger.warning("[benchmark] custom grader failed: %s", exc)
                return 0.0

        if task.grading == "param_match":
            return self.grade_param_match(
                trace, task.expected_tools, task.expected_params
            )

        # Default: tool_called
        return self.grade_tool_called(trace, task.expected_tools)

    @staticmethod
    def grade_tool_called(trace: object | None, expected_tools: list[str]) -> float:
        """Grade: did the expected tools get called?"""
        if not expected_tools:
            return 1.0
        if trace is None:
            return 0.0
        steps = getattr(trace, "steps", [])
        called = {step.tool_id for step in steps}
        matched = sum(1 for t in expected_tools if t in called)
        return matched / len(expected_tools)

    @staticmethod
    def grade_param_match(
        trace: object | None,
        expected_tools: list[str],
        expected_params: dict[str, Any],
    ) -> float:
        """Grade: were the expected params passed to the expected tools?"""
        if not expected_params:
            return 1.0
        if trace is None:
            return 0.0

        steps = getattr(trace, "steps", [])
        # Collect all params from steps matching expected tools
        actual_params: dict[str, Any] = {}
        for step in steps:
            if not expected_tools or step.tool_id in expected_tools:
                actual_params.update(getattr(step, "params", {}))

        if not actual_params:
            return 0.0

        matched = sum(
            1
            for key, value in expected_params.items()
            if key in actual_params and actual_params[key] == value
        )
        return matched / len(expected_params)
