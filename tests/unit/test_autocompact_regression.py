"""Regression: context compaction never hides the tool result the model has
not read yet, and the loop budget comes from KernelConfig (report item E-1)."""

from __future__ import annotations

from pathlib import Path

from symbiote.config.models import KernelConfig
from tests.support import ScriptedLLM, add_tool, make_kernel, tool_call_block

MARK = "SECRET-4711"


def _script(calls_until_giveup: int = 6):
    def fn(i: int, messages: list[dict]) -> str:
        seen = "\n".join(str(m.get("content")) for m in messages[1:])
        if MARK in seen:
            return f"The balance is {MARK}"
        if i >= calls_until_giveup:
            return "could not get the balance"
        return tool_call_block("get_balance", account=f"acc-{i}")

    return fn


def _text(out) -> str:
    return out["text"] if isinstance(out, dict) else out


def test_big_system_prompt_does_not_hide_fresh_result(tmp_path: Path) -> None:
    llm = ScriptedLLM(_script())
    kernel = make_kernel(tmp_path, llm)
    sym = kernel.create_symbiote(
        name="a", role="assistant", persona={"instructions": "x" * 60_000}
    )
    runs: list[dict] = []
    add_tool(kernel, sym.id, "get_balance", lambda p: runs.append(p) or {"balance": MARK})
    session = kernel.start_session(sym.id)

    out = kernel.message(session.id, "what is the balance?")

    assert len(runs) == 1
    assert len(llm.calls) == 2
    assert MARK in _text(out)
    kernel.shutdown()


def test_single_huge_result_is_truncated_not_dropped(tmp_path: Path) -> None:
    llm = ScriptedLLM(_script())
    kernel = make_kernel(tmp_path, llm, tool_result_max_chars=200_000)
    sym = kernel.create_symbiote(name="a", role="assistant")
    runs: list[dict] = []
    add_tool(
        kernel,
        sym.id,
        "get_balance",
        lambda p: runs.append(p) or {"balance": MARK, "history": "n" * 120_000},
    )
    session = kernel.start_session(sym.id)

    out = kernel.message(session.id, "what is the balance?")

    assert len(runs) == 1
    assert MARK in _text(out)
    second_call = "\n".join(str(m.get("content")) for m in llm.calls[1]["messages"])
    assert len(second_call) < 120_000  # it was cut to fit the loop budget
    kernel.shutdown()


def test_older_steps_are_still_compacted(tmp_path: Path) -> None:
    def fn(i: int, messages: list[dict]) -> str:
        return tool_call_block("step", n=i) if i < 4 else "done"

    llm = ScriptedLLM(fn)
    kernel = make_kernel(tmp_path, llm, loop_context_budget=2_000)
    sym = kernel.create_symbiote(name="a", role="assistant")
    add_tool(kernel, sym.id, "step", lambda p: {"value": f"RESULT-{p['n']}", "pad": "p" * 3000})
    session = kernel.start_session(sym.id)

    kernel.message(session.id, "go")

    last = llm.calls[-1]["messages"]
    text = "\n".join(str(m.get("content")) for m in last)
    assert "RESULT-3" in text  # the latest result is visible
    assert "RESULT-0" not in text  # older ones were summarized
    kernel.shutdown()


def test_warns_when_result_ceiling_exceeds_loop_budget(tmp_path: Path, caplog) -> None:
    with caplog.at_level("WARNING", logger="symbiote.core.kernel"):
        make_kernel(tmp_path, ScriptedLLM("ok"), tool_result_max_chars=100_000).shutdown()
    assert "loop_context_budget" in caplog.text
    caplog.clear()
    with caplog.at_level("WARNING", logger="symbiote.core.kernel"):
        make_kernel(
            tmp_path, ScriptedLLM("ok"), tool_result_max_chars=100_000, loop_context_budget=80_000
        ).shutdown()
    assert "loop_context_budget" not in caplog.text


def test_loop_context_budget_reaches_the_runner(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"), loop_context_budget=1234)
    runner = kernel._runner_registry.select("chat")
    assert runner._context_budget == 1234
    assert KernelConfig().loop_context_budget == 16_000
    kernel.shutdown()


def test_context_estimate_counts_tools_and_extra_context(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sym = kernel.create_symbiote(name="a", role="assistant")
    for i in range(20):
        add_tool(
            kernel,
            sym.id,
            f"tool_{i}",
            lambda p: None,
            parameters={"type": "object", "properties": {"q": {"type": "string", "description": "d" * 400}}},
        )
    session = kernel.start_session(sym.id)

    ctx = kernel._context_assembler.build(
        symbiote_id=sym.id,
        session_id=session.id,
        user_input="hi",
        extra_context={"page": "z" * 40_000},
    )

    assert ctx.total_tokens_estimate > 10_000 // 1
    kernel.shutdown()
