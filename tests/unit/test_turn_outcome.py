"""The host learns why the tool loop stopped from the turn itself
(SymGateway feedback on 0.8.7: it had to read ``execution_traces``), and
``message()`` declares the dict it returns when the turn called tools."""

from __future__ import annotations

import asyncio
import typing
from pathlib import Path

from symbiote.core.kernel import SymbioteKernel
from symbiote.core.turn_result import STOP_REASONS, TurnOutcome, TurnReply
from symbiote.environment.descriptors import ToolCallResult
from tests.support import ScriptedLLM, add_tool, make_kernel, tool_call_block


def _kernel(tmp_path: Path, script) -> tuple[SymbioteKernel, str]:
    kernel = make_kernel(tmp_path, ScriptedLLM(script))
    symbiote = kernel.create_symbiote(name="a", role="assistant")
    return kernel, symbiote.id


def _fails(_params: dict) -> dict:
    raise RuntimeError("upstream down")


class TestTurnOutcome:
    def test_a_plain_answer_ends_the_turn(self, tmp_path: Path) -> None:
        kernel, sym = _kernel(tmp_path, "hello")
        try:
            outcome = kernel.turn(kernel.start_session(sym).id, "hi")
        finally:
            kernel.shutdown()

        assert isinstance(outcome, TurnOutcome)
        assert outcome.text == "hello"
        assert outcome.stop_reason == "end_turn"
        assert not outcome.stopped_early
        assert (outcome.tool_calls, outcome.tool_results) == (0, [])

    def test_a_tool_turn_brings_its_results_and_counts(self, tmp_path: Path) -> None:
        def script(i: int, _messages: list[dict]) -> str:
            return tool_call_block("lookup") if i == 0 else "found it"

        kernel, sym = _kernel(tmp_path, script)
        add_tool(kernel, sym, "lookup", lambda _p: {"value": 42})
        try:
            outcome = kernel.turn(kernel.start_session(sym).id, "look")
        finally:
            kernel.shutdown()

        assert outcome.text == "found it"
        assert outcome.stop_reason == "end_turn"
        assert outcome.tool_calls == 1
        assert outcome.iterations >= 1
        assert len(outcome.tool_results) == 1
        result = outcome.tool_results[0]
        assert isinstance(result, ToolCallResult)
        assert (result.tool_id, result.success) == ("lookup", True)

    def test_a_loop_ended_by_a_limit_says_which(self, tmp_path: Path) -> None:
        def script(i: int, _messages: list[dict]) -> str:
            return tool_call_block("flaky") if i < 3 else "gave up"

        kernel, sym = _kernel(tmp_path, script)
        add_tool(kernel, sym, "flaky", _fails)
        try:
            session = kernel.start_session(sym)
            outcome = kernel.turn(session.id, "try it")
            stored = kernel._latest_trace(session.id)
        finally:
            kernel.shutdown()

        assert outcome.stop_reason == "circuit_breaker"
        assert outcome.stopped_early
        assert outcome.tool_calls == 3
        assert [r.success for r in outcome.tool_results] == [False, False, False]
        assert stored is not None and stored.stop_reason == outcome.stop_reason

    def test_every_reason_the_loops_set_is_documented(self) -> None:
        for reason in ("end_turn", "max_iterations", "stagnation", "circuit_breaker",
                       "timeout", "on_after_tool_result", "block_failure"):
            assert reason in STOP_REASONS

    def test_turn_async_matches_turn(self, tmp_path: Path) -> None:
        def script(i: int, _messages: list[dict]) -> str:
            return tool_call_block("lookup") if i == 0 else "async done"

        kernel, sym = _kernel(tmp_path, script)
        add_tool(kernel, sym, "lookup", lambda _p: "x")
        tokens: list[str] = []
        try:
            outcome = asyncio.run(
                kernel.turn_async(kernel.start_session(sym).id, "go", on_token=tokens.append)
            )
        finally:
            kernel.shutdown()

        assert outcome.text == "async done"
        assert outcome.stop_reason == "end_turn"
        assert outcome.tool_calls == 1


class TestMessageReturnShape:
    def test_message_returns_text_or_a_turn_reply(self, tmp_path: Path) -> None:
        def script(i: int, messages: list[dict]) -> str:
            text = "\n".join(str(m.get("content")) for m in messages)
            if "use the tool" in text and "[Tool result: lookup]" not in text:
                return tool_call_block("lookup")
            return "done"

        kernel, sym = _kernel(tmp_path, script)
        add_tool(kernel, sym, "lookup", lambda _p: "x")
        try:
            session = kernel.start_session(sym)
            plain = kernel.message(session.id, "just talk")
            with_tools = asyncio.run(kernel.message_async(session.id, "use the tool"))
        finally:
            kernel.shutdown()

        assert plain == "done"
        assert isinstance(with_tools, dict)
        assert set(with_tools) == set(TurnReply.__annotations__)
        assert with_tools["text"] == "done"
        assert with_tools["tool_results"][0]["tool_id"] == "lookup"

    def test_the_annotations_declare_both_shapes(self) -> None:
        for name in ("message", "message_async"):
            hints = typing.get_type_hints(getattr(SymbioteKernel, name))
            assert set(typing.get_args(hints["return"])) == {str, TurnReply}
