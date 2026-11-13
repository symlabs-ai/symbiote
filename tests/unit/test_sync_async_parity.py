"""Regression: run() and run_async() behave the same for the same scenario
(report item E-11). This is also the safety net for unifying both loops."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from symbiote.runners.chat import ChatRunner
from tests.support import ScriptedLLM, add_tool, make_kernel, tool_call_block


def _fn(i: int, messages: list[dict]) -> str:
    if "[Tool result" in str(messages[-1].get("content")):
        return "LLM final text"
    return tool_call_block("create_order", item="x", n=i)


class Review:
    def __init__(self) -> None:
        self.spawned = 0

    def spawn(self, session_id, symbiote_id) -> None:
        self.spawned += 1


def _build(tmp_path: Path, **runner_kwargs):
    llm = ScriptedLLM(_fn)
    kernel = make_kernel(tmp_path, llm)
    sym = kernel.create_symbiote(name="a", role="assistant")
    add_tool(kernel, sym.id, "create_order", lambda p: {"order_id": 42})
    runner = ChatRunner(llm, tool_gateway=kernel.tool_gateway, **runner_kwargs)
    session = kernel.start_session(sym.id)
    ctx = kernel._context_assembler.build(
        session_id=session.id, symbiote_id=sym.id, user_input="create the order"
    )
    return kernel, llm, runner, ctx


def _run(runner: ChatRunner, ctx, mode: str, **kw):
    if mode == "sync":
        return runner.run(ctx, **kw)
    return asyncio.run(runner.run_async(ctx, **kw))


def _summary(result, llm) -> tuple:
    text = result.output["text"] if isinstance(result.output, dict) else result.output
    stop = result.loop_trace.stop_reason if result.loop_trace else None
    return text, stop, len(llm.calls)


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_on_after_tool_result_ends_the_loop(tmp_path: Path, mode: str) -> None:
    hook_calls: list = []
    kernel, llm, runner, ctx = _build(
        tmp_path,
        on_after_tool_result=lambda calls, results: hook_calls.append(1) or "HOOK FINAL TEXT",
    )

    result = _run(runner, ctx, mode)

    assert _summary(result, llm) == ("HOOK FINAL TEXT", "on_after_tool_result", 1)
    assert hook_calls == [1]
    kernel.shutdown()


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_skill_review_nudge_fires(tmp_path: Path, mode: str) -> None:
    review = Review()
    kernel, llm, runner, ctx = _build(
        tmp_path, background_review=review, skill_nudge_interval=1
    )

    _run(runner, ctx, mode)

    assert review.spawned == 1
    kernel.shutdown()


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_zero_max_iterations(tmp_path: Path, mode: str) -> None:
    kernel, llm, runner, ctx = _build(tmp_path)
    ctx0 = ctx.model_copy(update={"max_tool_iterations": 0})

    result = _run(runner, ctx0, mode)

    assert result.success is True
    kernel.shutdown()


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_instant_mode_streams_only_the_clean_text(tmp_path: Path, mode: str) -> None:
    class StreamingLLM(ScriptedLLM):
        def stream(self, messages, config=None, tools=None):
            text = self.complete(messages, config, tools)
            yield from (text[i : i + 7] for i in range(0, len(text), 7))

    llm = StreamingLLM(lambda i, m: "I will create it.\n" + tool_call_block("create_order", item="x"))
    kernel = make_kernel(tmp_path, llm)
    sym = kernel.create_symbiote(name="a", role="assistant")
    add_tool(kernel, sym.id, "create_order", lambda p: {"order_id": 42}, tool_mode="instant")
    session = kernel.start_session(sym.id)
    ctx = kernel._context_assembler.build(
        session_id=session.id, symbiote_id=sym.id, user_input="create the order"
    )
    runner = ChatRunner(llm, tool_gateway=kernel.tool_gateway)
    streamed: list[str] = []

    result = _run(runner, ctx, mode, on_token=streamed.append)

    returned = result.output["text"] if isinstance(result.output, dict) else result.output
    assert "tool_call" not in "".join(streamed)
    assert "".join(streamed) == returned
    kernel.shutdown()
