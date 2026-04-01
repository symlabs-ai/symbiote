"""Tests for B-30 — Working Memory loop summary feature."""

from __future__ import annotations

import pytest

from symbiote.core.context import AssembledContext
from symbiote.core.models import Message
from symbiote.environment.descriptors import ToolCallResult
from symbiote.memory.working import WorkingMemory
from symbiote.runners.chat import ChatRunner

# ── Fake LLM ────────────────────────────────────────────────────────────────


class FakeLLM:
    """Implements LLMPort, returns a canned response."""

    def __init__(self, response: str = "Hello from LLM") -> None:
        self.response = response
        self.last_messages: list[dict] | None = None

    def complete(self, messages: list[dict], config: dict | None = None, tools: list[dict] | None = None) -> str:
        self.last_messages = messages
        return self.response


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_context(user_input: str = "Hi there") -> AssembledContext:
    return AssembledContext(
        symbiote_id="sym-1",
        session_id="sess-1",
        persona=None,
        working_memory_snapshot=None,
        relevant_memories=[],
        relevant_knowledge=[],
        user_input=user_input,
    )


# ── _build_loop_summary tests ───────────────────────────────────────────────


class TestBuildLoopSummary:
    """Unit tests for ChatRunner._build_loop_summary static method."""

    def test_empty_list_returns_empty_string(self):
        assert ChatRunner._build_loop_summary([]) == ""

    def test_single_successful_call(self):
        results = [
            ToolCallResult(tool_id="items_search", success=True, output={"items": []}),
        ]
        summary = ChatRunner._build_loop_summary(results)
        assert "[Loop summary: 1 tool calls]" in summary
        assert "1) items_search" in summary
        assert "ok" in summary

    def test_single_failed_call(self):
        results = [
            ToolCallResult(tool_id="items_publish", success=False, error="Permission denied"),
        ]
        summary = ChatRunner._build_loop_summary(results)
        assert "error: Permission denied" in summary

    def test_mixed_success_and_failure(self):
        results = [
            ToolCallResult(tool_id="items_search", success=True, output=[1, 2]),
            ToolCallResult(tool_id="items_get", success=True, output={"id": 42}),
            ToolCallResult(tool_id="items_publish", success=False, error="Timeout"),
        ]
        summary = ChatRunner._build_loop_summary(results)
        assert "[Loop summary: 3 tool calls]" in summary
        assert "1) items_search" in summary
        assert "2) items_get" in summary
        assert "3) items_publish" in summary
        # First two are ok, third is error
        lines = summary.split("\n")
        assert "ok" in lines[1]
        assert "ok" in lines[2]
        assert "error: Timeout" in lines[3]

    def test_error_truncated_at_50_chars(self):
        long_error = "A" * 100
        results = [
            ToolCallResult(tool_id="broken_tool", success=False, error=long_error),
        ]
        summary = ChatRunner._build_loop_summary(results)
        # Error should be truncated to 50 chars
        assert f"error: {'A' * 50}" in summary
        assert "A" * 51 not in summary

    def test_none_error_treated_as_empty(self):
        results = [
            ToolCallResult(tool_id="broken_tool", success=False, error=None),
        ]
        summary = ChatRunner._build_loop_summary(results)
        assert "error: " in summary

    def test_summary_format_is_compact(self):
        results = [
            ToolCallResult(tool_id="search", success=True, output="lots of data" * 100),
        ]
        summary = ChatRunner._build_loop_summary(results)
        # Summary should NOT contain tool output content
        assert "lots of data" not in summary
        # Should be compact — just header + one line
        lines = summary.strip().split("\n")
        assert len(lines) == 2


# ── Integration: working memory saving ──────────────────────────────────────


class TestWorkingMemoryLoopSummary:
    """Integration tests: loop summary is saved to WorkingMemory."""

    def test_no_tool_calls_saves_only_final_text(self):
        """Backward compat: no tools means only the response text is saved."""
        wm = WorkingMemory(session_id="sess-1")
        llm = FakeLLM(response="Just a chat reply")
        runner = ChatRunner(llm=llm, working_memory=wm)

        result = runner.run(_make_context("hello"))

        assert result.success
        # Working memory should have the assistant message
        msgs = [m for m in wm.recent_messages if m.role == "assistant"]
        assert len(msgs) == 1
        assert msgs[0].content == "Just a chat reply"
        # No loop summary prefix
        assert "[Loop summary" not in msgs[0].content

    def test_with_tool_calls_saves_summary_plus_final_text(self):
        """When tool calls happened, summary is prepended to the saved text."""
        wm = WorkingMemory(session_id="sess-1")

        # LLM returns a tool call first, then a final response
        responses = [
            '```tool_call\n{"tool": "items_search", "params": {"q": "test"}}\n```',
            "Found 3 items matching your query.",
        ]
        call_count = 0

        class MultiLLM:
            def complete(self, messages, config=None, tools=None):
                nonlocal call_count
                resp = responses[call_count]
                call_count += 1
                return resp

        class FakeGateway:
            def execute_tool_calls(self, symbiote_id, session_id, calls, timeout=None):
                return [
                    ToolCallResult(tool_id="items_search", success=True, output={"count": 3}),
                ]

            def get_risk_level(self, tool_id):
                return "low"

        runner = ChatRunner(
            llm=MultiLLM(),
            working_memory=wm,
            tool_gateway=FakeGateway(),
        )
        ctx = _make_context("search for test")
        ctx.available_tools = [{"tool_id": "items_search", "name": "Search", "description": "Search items"}]

        result = runner.run(ctx)

        assert result.success
        msgs = [m for m in wm.recent_messages if m.role == "assistant"]
        assert len(msgs) == 1
        content = msgs[0].content
        # Should have the loop summary
        assert "[Loop summary: 1 tool calls]" in content
        assert "items_search" in content
        assert "ok" in content
        # Should also have the final text
        assert "Found 3 items matching your query." in content

    def test_output_field_unchanged_with_tool_calls(self):
        """RunResult.output should contain final_text, not the memory_text."""
        wm = WorkingMemory(session_id="sess-1")

        responses = [
            '```tool_call\n{"tool": "do_thing", "params": {}}\n```',
            "Done.",
        ]
        call_count = 0

        class MultiLLM:
            def complete(self, messages, config=None, tools=None):
                nonlocal call_count
                resp = responses[call_count]
                call_count += 1
                return resp

        class FakeGateway:
            def execute_tool_calls(self, symbiote_id, session_id, calls, timeout=None):
                return [ToolCallResult(tool_id="do_thing", success=True, output="ok")]

            def get_risk_level(self, tool_id):
                return "low"

        runner = ChatRunner(
            llm=MultiLLM(),
            working_memory=wm,
            tool_gateway=FakeGateway(),
        )
        ctx = _make_context("do the thing")
        ctx.available_tools = [{"tool_id": "do_thing", "name": "Do", "description": "Does thing"}]

        result = runner.run(ctx)

        # output should have the raw final_text, not the memory_text with summary
        assert result.output["text"] == "Done."
        assert "[Loop summary" not in result.output["text"]
