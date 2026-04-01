"""Tests for streaming mid-loop callbacks: on_progress and on_stream (B-27)."""

from __future__ import annotations

import pytest

from symbiote.core.context import AssembledContext
from symbiote.environment.descriptors import ToolCallResult
from symbiote.runners.chat import ChatRunner

# ── Fake LLM that simulates a multi-iteration tool loop ──────────────────────


class _MultiStepLLM:
    """LLM that returns tool calls for the first N calls, then a final response.

    Simulates a tool loop where the LLM calls a tool, gets the result,
    and then produces a final text response.
    """

    def __init__(self, tool_responses: list[str], final_response: str) -> None:
        self._tool_responses = list(tool_responses)
        self._final_response = final_response
        self._call_idx = 0

    def complete(
        self,
        messages: list[dict],
        config: dict | None = None,
        tools: list[dict] | None = None,
    ) -> str:
        idx = self._call_idx
        self._call_idx += 1
        if idx < len(self._tool_responses):
            return self._tool_responses[idx]
        return self._final_response


class _SimpleLLM:
    """LLM that returns a single plain response (no tool calls)."""

    def __init__(self, response: str = "Final answer.") -> None:
        self._response = response

    def complete(
        self,
        messages: list[dict],
        config: dict | None = None,
        tools: list[dict] | None = None,
    ) -> str:
        return self._response


# ── Fake ToolGateway ─────────────────────────────────────────────────────────


class _FakeToolGateway:
    """Minimal ToolGateway that returns success for any tool call."""

    def execute_tool_calls(self, *, symbiote_id, session_id, calls, timeout=None):
        return [
            ToolCallResult(tool_id=c.tool_id, success=True, output="ok")
            for c in calls
        ]

    async def execute_tool_calls_async(self, *, symbiote_id, session_id, calls, timeout=None):
        return self.execute_tool_calls(
            symbiote_id=symbiote_id,
            session_id=session_id,
            calls=calls,
            timeout=timeout,
        )

    def get_risk_level(self, tool_id: str) -> str:
        return "low"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_context(
    user_input: str = "Do something",
    tool_loop: bool = True,
    max_tool_iterations: int = 10,
) -> AssembledContext:
    return AssembledContext(
        symbiote_id="sym-1",
        session_id="sess-1",
        user_input=user_input,
        available_tools=[
            {"tool_id": "search", "name": "Search", "description": "Search things", "parameters": {"q": {"type": "string"}}},
        ],
        tool_loop=tool_loop,
        max_tool_iterations=max_tool_iterations,
    )


def _tool_call_response(text: str, tool_id: str = "search", params: str = '{"q": "test"}') -> str:
    """Build an LLM response that includes narration + a tool call."""
    return f"{text}\n\n```tool_call\n{{\"tool\": \"{tool_id}\", \"params\": {params}}}\n```"


# ── Tests: on_progress ───────────────────────────────────────────────────────


class TestOnProgress:
    def test_receives_events_during_multi_iteration_loop(self) -> None:
        """on_progress receives iteration_start/tool_start/tool_end/iteration_end events."""
        events: list[tuple[str, int, int]] = []

        def on_progress(event_type: str, iteration: int, total: int) -> None:
            events.append((event_type, iteration, total))

        llm = _MultiStepLLM(
            tool_responses=[
                _tool_call_response("Vou verificar..."),
                _tool_call_response("Ainda verificando..."),
            ],
            final_response="Aqui esta o resultado.",
        )
        runner = ChatRunner(
            llm=llm,
            tool_gateway=_FakeToolGateway(),
            on_progress=on_progress,
        )
        ctx = _make_context()
        result = runner.run(ctx)

        assert result.success is True

        # Should have events for 3 iterations:
        # iter 1: iteration_start, tool_start, tool_end, iteration_end
        # iter 2: iteration_start, tool_start, tool_end, iteration_end
        # iter 3 (final): iteration_start, iteration_end
        event_types = [e[0] for e in events]
        assert "iteration_start" in event_types
        assert "tool_start" in event_types
        assert "tool_end" in event_types
        assert "iteration_end" in event_types

    def test_event_ordering_is_correct(self) -> None:
        """Events follow: iteration_start -> tool_start -> tool_end -> iteration_end."""
        events: list[tuple[str, int, int]] = []

        def on_progress(event_type: str, iteration: int, total: int) -> None:
            events.append((event_type, iteration, total))

        llm = _MultiStepLLM(
            tool_responses=[_tool_call_response("Checking...")],
            final_response="Done.",
        )
        runner = ChatRunner(
            llm=llm,
            tool_gateway=_FakeToolGateway(),
            on_progress=on_progress,
        )
        result = runner.run(_make_context())
        assert result.success is True

        # Iteration 1 (has tool calls): iteration_start, tool_start, tool_end, iteration_end
        # Iteration 2 (final): iteration_start, iteration_end
        iter1_events = [e[0] for e in events if e[1] == 1]
        assert iter1_events == ["iteration_start", "tool_start", "tool_end", "iteration_end"]

        iter2_events = [e[0] for e in events if e[1] == 2]
        assert iter2_events == ["iteration_start", "iteration_end"]

    def test_iteration_numbers_are_1_based(self) -> None:
        """Iteration numbers start at 1, not 0."""
        events: list[tuple[str, int, int]] = []

        def on_progress(event_type: str, iteration: int, total: int) -> None:
            events.append((event_type, iteration, total))

        llm = _MultiStepLLM(
            tool_responses=[_tool_call_response("Step 1")],
            final_response="Final.",
        )
        runner = ChatRunner(
            llm=llm,
            tool_gateway=_FakeToolGateway(),
            on_progress=on_progress,
        )
        runner.run(_make_context())

        iterations = {e[1] for e in events}
        assert 0 not in iterations
        assert 1 in iterations
        assert 2 in iterations

    def test_none_on_progress_no_crash(self) -> None:
        """When on_progress is None, no crash occurs."""
        llm = _MultiStepLLM(
            tool_responses=[_tool_call_response("Check...")],
            final_response="Done.",
        )
        runner = ChatRunner(
            llm=llm,
            tool_gateway=_FakeToolGateway(),
            on_progress=None,
        )
        result = runner.run(_make_context())
        assert result.success is True

    def test_no_tool_loop_still_emits_iteration_events(self) -> None:
        """Even without tool calls, iteration_start and iteration_end are emitted."""
        events: list[tuple[str, int, int]] = []

        def on_progress(event_type: str, iteration: int, total: int) -> None:
            events.append((event_type, iteration, total))

        llm = _SimpleLLM("Simple answer.")
        runner = ChatRunner(llm=llm, on_progress=on_progress)
        result = runner.run(_make_context(tool_loop=False))

        assert result.success is True
        event_types = [e[0] for e in events]
        assert event_types == ["iteration_start", "iteration_end"]


# ── Tests: on_stream ─────────────────────────────────────────────────────────


class TestOnStream:
    def test_receives_text_from_each_iteration(self) -> None:
        """on_stream receives text from every iteration, including intermediate ones."""
        stream_events: list[tuple[str, int]] = []

        def on_stream(text: str, iteration: int) -> None:
            stream_events.append((text, iteration))

        llm = _MultiStepLLM(
            tool_responses=[
                _tool_call_response("Vou verificar...", params='{"q": "step1"}'),
                _tool_call_response("Ainda verificando...", params='{"q": "step2"}'),
            ],
            final_response="Aqui esta o resultado.",
        )
        runner = ChatRunner(
            llm=llm,
            tool_gateway=_FakeToolGateway(),
            on_stream=on_stream,
        )
        result = runner.run(_make_context())
        assert result.success is True

        # Should have 3 stream events (one per iteration)
        assert len(stream_events) == 3
        # Iteration 1: intermediate narration
        assert stream_events[0][1] == 1
        assert "Vou verificar" in stream_events[0][0]
        # Iteration 2: intermediate narration
        assert stream_events[1][1] == 2
        assert "Ainda verificando" in stream_events[1][0]
        # Iteration 3: final response
        assert stream_events[2][1] == 3
        assert "Aqui esta o resultado" in stream_events[2][0]

    def test_none_on_stream_no_crash(self) -> None:
        """When on_stream is None, no crash occurs."""
        llm = _MultiStepLLM(
            tool_responses=[_tool_call_response("Check...")],
            final_response="Done.",
        )
        runner = ChatRunner(
            llm=llm,
            tool_gateway=_FakeToolGateway(),
            on_stream=None,
        )
        result = runner.run(_make_context())
        assert result.success is True

    def test_on_stream_receives_iteration_number(self) -> None:
        """on_stream iteration numbers are 1-based."""
        stream_events: list[tuple[str, int]] = []

        def on_stream(text: str, iteration: int) -> None:
            stream_events.append((text, iteration))

        llm = _SimpleLLM("Final answer.")
        runner = ChatRunner(llm=llm, on_stream=on_stream)
        result = runner.run(_make_context(tool_loop=False))

        assert result.success is True
        assert len(stream_events) == 1
        assert stream_events[0][1] == 1  # 1-based


# ── Tests: backward compatibility ────────────────────────────────────────────


class TestBackwardCompatibility:
    def test_on_token_only_receives_final_response(self) -> None:
        """on_token is NOT called for intermediate iterations (backward compat)."""
        token_events: list[str] = []

        def on_token(text: str) -> None:
            token_events.append(text)

        llm = _MultiStepLLM(
            tool_responses=[
                _tool_call_response("Vou verificar..."),
            ],
            final_response="Resultado final.",
        )
        runner = ChatRunner(
            llm=llm,
            tool_gateway=_FakeToolGateway(),
        )
        result = runner.run(_make_context(), on_token=on_token)
        assert result.success is True

        # on_token should only have the final response
        full_text = "".join(token_events)
        assert "Resultado final" in full_text
        assert "Vou verificar" not in full_text

    def test_on_token_and_on_stream_coexist(self) -> None:
        """Both on_token and on_stream can be set simultaneously."""
        token_events: list[str] = []
        stream_events: list[tuple[str, int]] = []

        def on_token(text: str) -> None:
            token_events.append(text)

        def on_stream(text: str, iteration: int) -> None:
            stream_events.append((text, iteration))

        llm = _MultiStepLLM(
            tool_responses=[_tool_call_response("Checking...")],
            final_response="Final.",
        )
        runner = ChatRunner(
            llm=llm,
            tool_gateway=_FakeToolGateway(),
            on_stream=on_stream,
        )
        result = runner.run(_make_context(), on_token=on_token)
        assert result.success is True

        # on_stream gets both iterations
        assert len(stream_events) == 2
        # on_token only gets the final
        full_text = "".join(token_events)
        assert "Final" in full_text
        assert "Checking" not in full_text

    def test_no_callbacks_no_crash(self) -> None:
        """Runner works fine with no callbacks at all."""
        llm = _MultiStepLLM(
            tool_responses=[_tool_call_response("Step...")],
            final_response="Done.",
        )
        runner = ChatRunner(llm=llm, tool_gateway=_FakeToolGateway())
        result = runner.run(_make_context())
        assert result.success is True
        assert "Done." in str(result.output)
