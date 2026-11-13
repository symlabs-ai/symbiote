"""Regression: a loop that runs out of iterations or time still answers the
user, and iterations count model rounds, not tool calls (report item E-6)."""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from symbiote.runners import chat as chat_mod
from symbiote.runners.loop_control import LoopController
from tests.support import ScriptedLLM, add_tool, make_kernel, tool_call_block


def _text(out) -> str:
    return out["text"] if isinstance(out, dict) else out


def _setup(tmp_path: Path, fn, **configure):
    llm = ScriptedLLM(fn)
    kernel = make_kernel(tmp_path, llm)
    sym = kernel.create_symbiote(name="a", role="assistant")
    add_tool(kernel, sym.id, "fetch", lambda p: {"page": p.get("n")}, **configure)
    return kernel, llm, kernel.start_session(sym.id)


def _last_trace(kernel, session_id: str) -> dict:
    return kernel._storage.fetch_one(
        "SELECT * FROM execution_traces WHERE session_id = ? ORDER BY created_at DESC",
        (session_id,),
    )


def test_exhausted_loop_gets_a_final_answer(tmp_path: Path) -> None:
    def fn(i: int, messages: list[dict]) -> str:
        if "tool-call limit" in str(messages[-1].get("content")):
            return "Here is what I found so far."
        return tool_call_block("fetch", n=i)

    kernel, llm, session = _setup(tmp_path, fn, max_tool_iterations=3)

    out = kernel.message(session.id, "research")

    assert _text(out) == "Here is what I found so far."
    assert len(llm.calls) == 4  # 3 rounds + 1 final answer without tools
    kernel.shutdown()


def test_stubborn_model_still_yields_non_empty_text(tmp_path: Path) -> None:
    kernel, _llm, session = _setup(
        tmp_path, lambda i, m: tool_call_block("fetch", n=i), max_tool_iterations=3
    )

    out = kernel.message(session.id, "research")

    assert _text(out).strip()
    stored = kernel._storage.fetch_all(
        "SELECT content FROM messages WHERE session_id = ? AND role = 'assistant'",
        (session.id,),
    )
    assert stored and all(r["content"].strip() for r in stored)
    kernel.shutdown()


def test_narration_with_tool_call_is_not_a_final_answer(tmp_path: Path) -> None:
    kernel, _llm, session = _setup(
        tmp_path,
        lambda i, m: "Let me check one more source...\n" + tool_call_block("fetch", n=i),
        max_tool_iterations=2,
    )

    out = kernel.message(session.id, "research")

    assert "one more source" not in _text(out)
    assert _text(out).strip()
    kernel.shutdown()


def test_parallel_calls_in_one_round_count_as_one_iteration(tmp_path: Path) -> None:
    def fn(i: int, messages: list[dict]) -> str:
        if i == 0:
            return "\n".join(tool_call_block("fetch", n=j) for j in range(12))
        return "Summary of 12 pages."

    kernel, llm, session = _setup(tmp_path, fn, max_tool_iterations=10)

    out = kernel.message(session.id, "read all")

    assert _text(out) == "Summary of 12 pages."
    assert len(llm.calls) == 2
    trace = _last_trace(kernel, session.id)
    assert trace["total_iterations"] == 2
    assert trace["total_tool_calls"] == 12
    kernel.shutdown()


def test_timeout_reports_real_rounds_and_answers(tmp_path: Path, monkeypatch) -> None:
    clock = itertools.count(start=0, step=3)  # every monotonic() call: +3 s
    monkeypatch.setattr(chat_mod.time, "monotonic", lambda: float(next(clock)))

    kernel, llm, session = _setup(
        tmp_path, lambda i, m: tool_call_block("fetch", n=i), loop_timeout=10
    )

    out = kernel.message(session.id, "research")

    assert _text(out).strip()
    trace = _last_trace(kernel, session.id)
    assert trace["stop_reason"] == "timeout"
    assert trace["total_iterations"] == len(llm.calls)
    kernel.shutdown()


@pytest.mark.parametrize(
    "override",
    [
        "Tool {tool} failed",
        'Tool failed, details: {"a": 1}',
        "Unbalanced { brace",
        "Tool {tool_id} failed {count} times",
    ],
)
def test_injection_override_never_raises(override: str) -> None:
    controller = LoopController(circuit_breaker_msg=override)
    for _ in range(3):
        controller.record("flaky", {}, False)
        controller.end_round()

    message = controller.get_injection_message()

    assert isinstance(message, str) and message
    if "{tool_id}" in override:
        assert message == "Tool flaky failed 3 times"
