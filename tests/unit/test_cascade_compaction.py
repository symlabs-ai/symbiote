"""Regression: a second loop compaction keeps what the first one summarized
(report item E-12)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from symbiote.environment.descriptors import LLMResponse, NativeToolCall
from tests.support import ScriptedLLM, add_tool, make_kernel, tool_call_block

N = 8


def _text_fn(i: int, messages: list[dict]):
    return tool_call_block("step", n=i) if i < N else "done"


def _native_fn(i: int, messages: list[dict]):
    if i < N:
        return LLMResponse(
            content="", tool_calls=[NativeToolCall(call_id=f"c{i}", tool_id="step", params={"n": i})]
        )
    return "done"


@pytest.mark.parametrize(("native", "fn"), [(False, _text_fn), (True, _native_fn)])
def test_every_step_stays_visible_after_two_compactions(tmp_path: Path, native, fn) -> None:
    llm = ScriptedLLM(fn)
    kernel = make_kernel(tmp_path, llm)
    kernel.set_native_tools(native)
    sym = kernel.create_symbiote(name="a", role="assistant")
    add_tool(
        kernel,
        sym.id,
        "step",
        lambda p: {"value": f"RESULT-OF-STEP-{p['n']}"},
        parameters={"type": "object", "properties": {"n": {"type": "integer"}}},
        max_tool_iterations=12,
    )
    session = kernel.start_session(sym.id)

    kernel.message(session.id, "run 8 steps")

    last = json.dumps(llm.calls[-1]["messages"][2:], ensure_ascii=False)
    visible = [n for n in range(N) if f"RESULT-OF-STEP-{n}" in last]
    assert visible == list(range(N))
    # One summary message, not summaries nested inside summaries.
    assert last.count("[Context compacted") <= 1
    kernel.shutdown()
