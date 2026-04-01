"""Tests for instant mode — mode-aware scoring, fast-path runner, tuner filtering."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from symbiote.core.context import AssembledContext
from symbiote.core.scoring import _score_instant, compute_auto_score
from symbiote.runners.base import LoopStep, LoopTrace, RunResult
from symbiote.runners.chat import ChatRunner

# ── Scoring ──────────────────────────────────────────────────────────────────


class TestInstantScoring:
    """compute_auto_score with tool_mode='instant'."""

    def test_no_trace_with_tools(self):
        score = compute_auto_score(None, tool_mode="instant", has_tools=True)
        assert score == 0.7

    def test_no_trace_without_tools(self):
        score = compute_auto_score(None, tool_mode="instant", has_tools=False)
        assert score == 0.8

    def test_tool_called_success(self):
        trace = LoopTrace(
            steps=[LoopStep(iteration=1, tool_id="search", success=True)],
            total_iterations=1, total_tool_calls=1,
            stop_reason="end_turn", tool_mode="instant",
        )
        assert compute_auto_score(trace, tool_mode="instant", has_tools=True) == 1.0

    def test_tool_called_failure(self):
        trace = LoopTrace(
            steps=[LoopStep(iteration=1, tool_id="publish", success=False, error="timeout")],
            total_iterations=1, total_tool_calls=1,
            stop_reason="end_turn", tool_mode="instant",
        )
        assert compute_auto_score(trace, tool_mode="instant", has_tools=True) == 0.3

    def test_no_tool_with_tools_available(self):
        trace = LoopTrace(steps=[], total_iterations=1, stop_reason="end_turn", tool_mode="instant")
        assert compute_auto_score(trace, tool_mode="instant", has_tools=True) == 0.7

    def test_no_tool_no_tools_available(self):
        trace = LoopTrace(steps=[], total_iterations=1, stop_reason="end_turn", tool_mode="instant")
        assert compute_auto_score(trace, tool_mode="instant", has_tools=False) == 0.9

    def test_brief_mode_calibrated(self):
        """Brief mode: 3 iterations = no penalty (multi-step is normal)."""
        trace = LoopTrace(
            steps=[LoopStep(iteration=1, tool_id="x", success=True)],
            total_iterations=3, total_tool_calls=3,
            stop_reason="end_turn", tool_mode="brief",
        )
        assert compute_auto_score(trace, tool_mode="brief", has_tools=True) == 1.0

    def test_continuous_relaxed_iterations(self):
        trace = LoopTrace(
            steps=[LoopStep(iteration=i, tool_id="x", success=True) for i in range(1, 9)],
            total_iterations=8, total_tool_calls=8,
            stop_reason="end_turn", tool_mode="continuous",
        )
        assert compute_auto_score(trace, tool_mode="continuous") == 0.8

    def test_backward_compat_no_tool_mode(self):
        """Calling without tool_mode uses brief (default) behavior."""
        trace = LoopTrace(
            steps=[LoopStep(iteration=1, tool_id="x", success=True)],
            total_iterations=1, total_tool_calls=1,
            stop_reason="end_turn",
        )
        assert compute_auto_score(trace) == 1.0


# ── Fast-path runner ─────────────────────────────────────────────────────────


class TestInstantFastPath:
    """ChatRunner delegates to _run_instant for instant mode."""

    def _make_context(self, tool_mode: str = "instant", **kwargs) -> AssembledContext:
        return AssembledContext(
            symbiote_id="test-sym",
            session_id="test-sess",
            user_input="hello",
            tool_mode=tool_mode,
            tool_loop=tool_mode != "instant",
            **kwargs,
        )

    def _make_runner(self, llm_response: str = "ok", **kwargs) -> ChatRunner:
        llm = MagicMock()
        llm.complete.return_value = llm_response
        # Ensure no stream attribute so _call_llm_sync uses complete()
        del llm.stream
        return ChatRunner(llm=llm, **kwargs)

    def test_instant_delegates_to_run_instant(self):
        runner = self._make_runner()
        ctx = self._make_context()
        with patch.object(runner, "_run_instant", return_value=RunResult(success=True, output="fast")) as mock:
            runner.run(ctx)
            mock.assert_called_once_with(ctx, None)

    def test_brief_uses_run_loop(self):
        runner = self._make_runner()
        ctx = self._make_context(tool_mode="brief")
        with patch.object(runner, "_run_loop", return_value=RunResult(success=True, output="loop")) as mock:
            runner.run(ctx)
            mock.assert_called_once_with(ctx, None)

    def test_instant_returns_trace_with_tool_mode(self):
        runner = self._make_runner(llm_response="done")
        ctx = self._make_context()
        result = runner.run(ctx)
        assert result.success
        assert result.loop_trace is not None
        assert result.loop_trace.tool_mode == "instant"
        assert result.loop_trace.stop_reason == "end_turn"
        assert result.loop_trace.total_iterations == 1

    def test_instant_no_progress_callbacks(self):
        progress_calls = []
        runner = self._make_runner(
            llm_response="ok",
            on_progress=lambda event, i, t: progress_calls.append(event),
        )
        ctx = self._make_context()
        runner.run(ctx)
        assert progress_calls == []

    def test_instant_fires_on_stream(self):
        stream_calls = []
        runner = self._make_runner(
            llm_response="streamed text",
            on_stream=lambda text, i: stream_calls.append((text, i)),
        )
        ctx = self._make_context()
        runner.run(ctx)
        assert len(stream_calls) == 1
        assert stream_calls[0] == ("streamed text", 1)

    def test_instant_no_loop_summary_in_memory(self):
        from symbiote.memory.working import WorkingMemory

        wm = WorkingMemory("test-sess")
        runner = self._make_runner(llm_response="direct answer")
        # Inject working_memory into runner
        runner._working_memory = wm
        ctx = self._make_context()
        runner.run(ctx)
        snap = wm.snapshot()
        # The working memory should have the raw text, no "[Loop summary:" prefix
        messages = snap.get("recent_messages", [])
        assert any(m.get("content") == "direct answer" for m in messages)
        assert not any("[Loop summary:" in m.get("content", "") for m in messages)

    def test_instant_async_delegates(self):
        runner = self._make_runner()
        ctx = self._make_context()
        with patch.object(runner, "_run_instant", return_value=RunResult(success=True, output="fast")) as mock:
            asyncio.run(runner.run_async(ctx))
            mock.assert_called_once_with(ctx, None)


# ── LoopTrace tool_mode field ────────────────────────────────────────────────


class TestLoopTraceToolMode:
    def test_default_brief(self):
        assert LoopTrace().tool_mode == "brief"

    def test_instant(self):
        assert LoopTrace(tool_mode="instant").tool_mode == "instant"

    def test_serialization(self):
        assert LoopTrace(tool_mode="continuous").model_dump()["tool_mode"] == "continuous"


# ── Context assembly ─────────────────────────────────────────────────────────


class TestInstantContextAssembly:
    """ContextAssembler adjusts behavior for instant mode."""

    def _make_assembler(self, tool_mode: str = "instant", memory_share: float = 0.40):
        from symbiote.core.context import ContextAssembler
        from symbiote.core.identity import IdentityManager
        from symbiote.core.models import MemoryEntry, Symbiote

        identity = MagicMock(spec=IdentityManager)
        identity.get.return_value = Symbiote(id="s1", name="test", role="assistant", persona_json={"name": "Bot"})

        memory = MagicMock()
        memory.get_relevant.return_value = [
            MemoryEntry(symbiote_id="s1", session_id="s", content="proc 1", type="procedural", importance=0.5, scope="global", source="system"),
            MemoryEntry(symbiote_id="s1", session_id="s", content="fact 1", type="factual", importance=0.8, scope="global", source="system"),
            MemoryEntry(symbiote_id="s1", session_id="s", content="fact 2", type="factual", importance=0.6, scope="global", source="system"),
        ]

        knowledge = MagicMock()
        knowledge.query.return_value = []

        env = MagicMock()
        env.get_tool_loading.return_value = "full"
        env.get_tool_mode.return_value = tool_mode
        env.get_tool_loop.return_value = tool_mode != "instant"
        env.get_prompt_caching.return_value = False
        env.get_memory_share.return_value = memory_share
        env.get_knowledge_share.return_value = 0.25
        env.get_max_tool_iterations.return_value = 10
        env.get_tool_call_timeout.return_value = 30.0
        env.get_loop_timeout.return_value = 300.0
        env.get_context_mode.return_value = "packed"
        env.get_tool_tags.return_value = None

        return ContextAssembler(
            identity=identity, memory=memory, knowledge=knowledge,
            environment=env, context_budget=50000,
        )

    def test_instant_mode_set(self):
        assembler = self._make_assembler(tool_mode="instant")
        ctx = assembler.build("sess", "s1", "hello")
        assert ctx.tool_mode == "instant"

    def test_instant_procedural_priority(self):
        """Instant mode sorts procedural memories before declarative."""
        assembler = self._make_assembler(tool_mode="instant")
        ctx = assembler.build("sess", "s1", "hello")
        if ctx.relevant_memories:
            first = ctx.relevant_memories[0]
            assert first["type"] == "procedural"

    def test_brief_does_not_reorder(self):
        """Brief mode keeps importance-based ordering."""
        assembler = self._make_assembler(tool_mode="brief")
        ctx = assembler.build("sess", "s1", "hello")
        if ctx.relevant_memories:
            first = ctx.relevant_memories[0]
            # importance 0.8 (factual) should be first in brief
            assert first["type"] == "factual"


# ── Harness versions per-mode ────────────────────────────────────────────────


class TestVersionsPerMode:
    """HarnessVersionRepository resolves mode-specific keys."""

    @pytest.fixture
    def repo(self, tmp_path):
        from symbiote.adapters.storage.sqlite import SQLiteAdapter
        from symbiote.harness.versions import HarnessVersionRepository
        adapter = SQLiteAdapter(db_path=tmp_path / "test.db")
        adapter.init_schema()
        return HarnessVersionRepository(adapter)

    def test_mode_specific_found(self, repo):
        repo.create_version("s1", "tool_instructions", "generic instructions")
        repo.create_version("s1", "tool_instructions:instant", "instant instructions")
        assert repo.get_active("s1", "tool_instructions", tool_mode="instant") == "instant instructions"

    def test_mode_specific_not_found_falls_back(self, repo):
        repo.create_version("s1", "tool_instructions", "generic instructions")
        assert repo.get_active("s1", "tool_instructions", tool_mode="instant") == "generic instructions"

    def test_no_mode_param_uses_generic(self, repo):
        repo.create_version("s1", "tool_instructions", "generic")
        repo.create_version("s1", "tool_instructions:instant", "instant specific")
        assert repo.get_active("s1", "tool_instructions") == "generic"

    def test_mode_specific_none_no_generic(self, repo):
        assert repo.get_active("s1", "tool_instructions", tool_mode="instant") is None


# ── Tuner mode filtering ─────────────────────────────────────────────────────


class TestTunerModeFiltering:
    """ParameterTuner excludes instant traces from iteration rules."""

    def _make_tuner(self, traces: list[dict], scores: list[dict] | None = None):
        from symbiote.harness.tuner import ParameterTuner

        storage = MagicMock()
        storage.fetch_all.side_effect = lambda sql, params: (
            traces if "execution_traces" in sql
            else (scores or [])
        )
        storage.fetch_one.return_value = {
            "max_tool_iterations": 10,
            "compaction_threshold": 4,
            "memory_share": 0.40,
            "knowledge_share": 0.25,
        }
        return ParameterTuner(storage)

    def test_instant_traces_excluded_from_count(self):
        """Instant traces are excluded from session_count (loop_traces only)."""
        traces = [
            {"stop_reason": "end_turn", "total_iterations": 1, "tool_mode": "instant"}
            for _ in range(10)
        ] + [
            {"stop_reason": "end_turn", "total_iterations": 3, "tool_mode": "brief"}
            for _ in range(5)
        ]
        tuner = self._make_tuner(traces)
        result = tuner.analyze("test-sym")
        # Only 5 brief traces count
        assert result.session_count == 5
        assert result.tier == 1  # 5 sessions = tier 1

    def test_all_instant_means_no_tuning(self):
        """If all traces are instant, session_count=0, no adjustments."""
        traces = [
            {"stop_reason": "end_turn", "total_iterations": 1, "tool_mode": "instant"}
            for _ in range(20)
        ]
        tuner = self._make_tuner(traces)
        result = tuner.analyze("test-sym")
        assert result.session_count == 0
        assert result.tier == 0
        assert result.adjustments == {}

    def test_brief_traces_still_analyzed(self):
        """Brief traces are analyzed normally even with instant traces present."""
        # 20 brief traces all hitting max_iterations (>80% threshold for tier 1)
        traces = [
            {"stop_reason": "max_iterations", "total_iterations": 10, "tool_mode": "brief"}
            for _ in range(5)
        ]
        tuner = self._make_tuner(traces)
        result = tuner.analyze("test-sym")
        assert result.session_count == 5
        assert result.tier == 1
        # 100% hit max_iterations → tier 1 threshold is 80% → should adjust
        assert "max_tool_iterations" in result.adjustments
