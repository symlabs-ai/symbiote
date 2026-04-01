"""Tests for H-11: BenchmarkRunner — task grading and suite aggregation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from symbiote.harness.benchmark import (
    BenchmarkResult,
    BenchmarkRunner,
    BenchmarkSuiteResult,
    BenchmarkTask,
)

# ── Fake trace objects ────────────────────────────────────────────────────────


@dataclass
class FakeStep:
    tool_id: str
    params: dict[str, Any] = field(default_factory=dict)
    iteration: int = 1
    success: bool = True
    error: str | None = None
    elapsed_ms: int = 10


@dataclass
class FakeTrace:
    steps: list[FakeStep] = field(default_factory=list)
    total_iterations: int = 0
    total_tool_calls: int = 0
    total_elapsed_ms: int = 0
    stop_reason: str | None = None


@dataclass
class FakeSession:
    id: str = "sess-1"


class FakeKernel:
    """Minimal kernel mock for benchmark tests."""

    def __init__(self, trace: FakeTrace | None = None) -> None:
        self._trace = trace
        self._last_trace: FakeTrace | None = None

    def start_session(self, symbiote_id: str, goal: str | None = None) -> FakeSession:
        return FakeSession()

    def message(self, session_id: str, content: str) -> str:
        self._last_trace = self._trace
        return "OK"

    def close_session(self, session_id: str) -> None:
        pass


# ── BenchmarkTask creation ────────────────────────────────────────────────────


class TestBenchmarkTaskDefaults:
    def test_defaults(self) -> None:
        task = BenchmarkTask(id="t1", description="Do something")
        assert task.expected_tools == []
        assert task.expected_params == {}
        assert task.grading == "tool_called"
        assert task.custom_grader is None
        assert task.timeout == 60.0

    def test_custom_values(self) -> None:
        task = BenchmarkTask(
            id="t2",
            description="Use tool X",
            expected_tools=["tool_x"],
            expected_params={"key": "val"},
            grading="param_match",
            timeout=30.0,
        )
        assert task.expected_tools == ["tool_x"]
        assert task.grading == "param_match"


# ── grade_tool_called ─────────────────────────────────────────────────────────


class TestGradeToolCalled:
    def test_correct_tools_score_1(self) -> None:
        trace = FakeTrace(steps=[FakeStep(tool_id="search"), FakeStep(tool_id="save")])
        score = BenchmarkRunner.grade_tool_called(trace, ["search", "save"])
        assert score == 1.0

    def test_missing_tools_score_0(self) -> None:
        trace = FakeTrace(steps=[FakeStep(tool_id="unrelated")])
        score = BenchmarkRunner.grade_tool_called(trace, ["search", "save"])
        assert score == 0.0

    def test_partial_match(self) -> None:
        trace = FakeTrace(steps=[FakeStep(tool_id="search")])
        score = BenchmarkRunner.grade_tool_called(trace, ["search", "save"])
        assert score == 0.5

    def test_no_expected_tools_score_1(self) -> None:
        trace = FakeTrace(steps=[FakeStep(tool_id="anything")])
        score = BenchmarkRunner.grade_tool_called(trace, [])
        assert score == 1.0

    def test_none_trace_score_0(self) -> None:
        score = BenchmarkRunner.grade_tool_called(None, ["search"])
        assert score == 0.0


# ── grade_param_match ─────────────────────────────────────────────────────────


class TestGradeParamMatch:
    def test_correct_params_score_1(self) -> None:
        trace = FakeTrace(
            steps=[FakeStep(tool_id="search", params={"query": "hello", "limit": 10})]
        )
        score = BenchmarkRunner.grade_param_match(
            trace, ["search"], {"query": "hello", "limit": 10}
        )
        assert score == 1.0

    def test_partial_params(self) -> None:
        trace = FakeTrace(
            steps=[FakeStep(tool_id="search", params={"query": "hello"})]
        )
        score = BenchmarkRunner.grade_param_match(
            trace, ["search"], {"query": "hello", "limit": 10}
        )
        assert score == 0.5

    def test_no_params_expected_score_1(self) -> None:
        trace = FakeTrace(steps=[FakeStep(tool_id="search")])
        score = BenchmarkRunner.grade_param_match(trace, ["search"], {})
        assert score == 1.0

    def test_none_trace_score_0(self) -> None:
        score = BenchmarkRunner.grade_param_match(None, ["search"], {"query": "x"})
        assert score == 0.0

    def test_wrong_tool_no_match(self) -> None:
        trace = FakeTrace(
            steps=[FakeStep(tool_id="unrelated", params={"query": "hello"})]
        )
        score = BenchmarkRunner.grade_param_match(
            trace, ["search"], {"query": "hello"}
        )
        assert score == 0.0


# ── run_task ──────────────────────────────────────────────────────────────────


class TestRunTask:
    def test_task_passes_when_correct_tools(self) -> None:
        trace = FakeTrace(
            steps=[FakeStep(tool_id="search")],
            total_iterations=1,
        )
        kernel = FakeKernel(trace=trace)
        runner = BenchmarkRunner(kernel)
        task = BenchmarkTask(id="t1", description="Search", expected_tools=["search"])

        result = runner.run_task("sym-1", task)

        assert result.passed is True
        assert result.score == 1.0
        assert result.tool_calls_made == ["search"]
        assert result.iterations == 1
        assert result.error is None

    def test_task_fails_when_wrong_tools(self) -> None:
        trace = FakeTrace(
            steps=[FakeStep(tool_id="wrong_tool")],
            total_iterations=1,
        )
        kernel = FakeKernel(trace=trace)
        runner = BenchmarkRunner(kernel)
        task = BenchmarkTask(id="t2", description="Search", expected_tools=["search"])

        result = runner.run_task("sym-1", task)

        assert result.passed is False
        assert result.score == 0.0
        assert result.tool_calls_made == ["wrong_tool"]

    def test_task_with_custom_grader(self) -> None:
        trace = FakeTrace(steps=[], total_iterations=0)
        kernel = FakeKernel(trace=trace)
        runner = BenchmarkRunner(kernel)

        def custom(t: object) -> float:
            return 0.75

        task = BenchmarkTask(
            id="t3", description="Custom", grading="custom", custom_grader=custom
        )
        result = runner.run_task("sym-1", task)

        assert result.score == 0.75
        assert result.passed is True

    def test_task_error_returns_zero(self) -> None:
        class BrokenKernel:
            _last_trace = None

            def start_session(self, *a, **kw):
                raise RuntimeError("boom")

        runner = BenchmarkRunner(BrokenKernel())
        task = BenchmarkTask(id="t4", description="Fail", expected_tools=["x"])

        result = runner.run_task("sym-1", task)
        assert result.passed is False
        assert result.score == 0.0
        assert result.error is not None


# ── run_suite ─────────────────────────────────────────────────────────────────


class TestRunSuite:
    def test_suite_aggregation(self) -> None:
        trace = FakeTrace(
            steps=[FakeStep(tool_id="search")],
            total_iterations=1,
        )
        kernel = FakeKernel(trace=trace)
        runner = BenchmarkRunner(kernel)

        tasks = [
            BenchmarkTask(id="t1", description="Search", expected_tools=["search"]),
            BenchmarkTask(id="t2", description="Save", expected_tools=["save"]),
        ]

        suite = runner.run_suite("sym-1", tasks, suite_name="regression")

        assert suite.suite_name == "regression"
        assert suite.symbiote_id == "sym-1"
        assert suite.total_tasks == 2
        assert suite.passed == 1  # t1 passes (search), t2 fails (save not called)
        assert suite.failed == 1
        assert suite.avg_score == 0.5
        assert len(suite.results) == 2
        assert suite.elapsed_ms >= 0


class TestBenchmarkSuiteResultStructure:
    def test_fields_present(self) -> None:
        result = BenchmarkSuiteResult(
            suite_name="test",
            symbiote_id="sym-1",
            total_tasks=3,
            passed=2,
            failed=1,
            avg_score=0.667,
            results=[],
            elapsed_ms=100,
        )
        assert result.suite_name == "test"
        assert result.total_tasks == 3
        assert result.passed == 2
        assert result.failed == 1
        assert result.avg_score == 0.667
        assert result.elapsed_ms == 100
