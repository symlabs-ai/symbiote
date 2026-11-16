"""Regression: tool_mode="long_run" runs the LongRunRunner, and its verdicts,
inputs and handoff are correct (report item E-2)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from symbiote.core.context import AssembledContext
from symbiote.environment.descriptors import LLMResponse
from symbiote.runners.long_run import LongRunRunner
from tests.support import ScriptedLLM, make_kernel

PLAN = json.dumps(
    [
        {"name": "A", "description": "d", "success_criteria": "c"},
        {"name": "B", "description": "d", "success_criteria": "c"},
    ]
)


def _system(messages: list[dict]) -> str:
    return str(messages[0].get("content"))


def _verdict(passed: bool) -> str:
    return json.dumps(
        {"passed": passed, "overall_score": 0.9 if passed else 0.1, "feedback": "f", "blocking_issues": []}
    )


def test_kernel_routes_long_run_mode(tmp_path: Path) -> None:
    def fn(i: int, messages: list[dict]) -> str:
        system = _system(messages)
        if system.startswith("PLANNER-PROMPT"):
            return PLAN
        if system.startswith("EVAL"):
            return _verdict(True)
        if system.startswith("You ran a multi-step project"):
            return "ANSWER: tudo feito"
        return "block done"

    llm = ScriptedLLM(fn)
    kernel = make_kernel(tmp_path, llm)
    sym = kernel.create_symbiote(name="a", role="assistant")
    kernel.environment.configure(
        symbiote_id=sym.id,
        tool_mode="long_run",
        planner_prompt="PLANNER-PROMPT: decompose",
        evaluator_prompt="EVAL",
    )
    session = kernel.start_session(sym.id)

    out = kernel.message(session.id, "build the whole system")

    assert any(_system(c["messages"]).startswith("PLANNER-PROMPT") for c in llm.calls)
    # The user reads the reply written from the block reports, which is
    # also what the conversation keeps; the counts stay in the handoff.
    assert out == "ANSWER: tudo feito"
    assert kernel._handoffs[session.id]["blocks_completed"] == 2
    assert kernel.inspector.list_messages(session.id)[-1]["content"] == out
    assert "long_run" in kernel._runner_registry.list_runners()
    assert session.id in kernel._handoffs
    kernel.shutdown()


def test_long_run_through_message_async(tmp_path: Path) -> None:
    def fn(i: int, messages: list[dict]) -> str:
        if _system(messages).startswith("You ran a multi-step project"):
            return "ANSWER"
        return PLAN if _system(messages).startswith("You are a project planner") else "done"

    kernel = make_kernel(tmp_path, ScriptedLLM(fn))
    sym = kernel.create_symbiote(name="a", role="assistant")
    kernel.environment.configure(symbiote_id=sym.id, tool_mode="long_run")
    session = kernel.start_session(sym.id)

    out = asyncio.run(kernel.message_async(session.id, "build it"))

    assert out == "ANSWER"
    kernel.shutdown()


def _ctx(**kw) -> AssembledContext:
    return AssembledContext(symbiote_id="s", session_id="x", user_input="build", **kw)


def test_rejected_blocks_are_failures() -> None:
    def fn(i: int, messages: list[dict]) -> str:
        system = _system(messages)
        if "project planner" in system:
            return PLAN
        if system.startswith("EVAL-PROMPT"):
            return _verdict(False)
        return "generator claims it is done"

    result = LongRunRunner(ScriptedLLM(fn)).run(_ctx(evaluator_prompt="EVAL-PROMPT"))

    assert result.success is False
    assert result.handoff_data["blocks_completed"] == 0
    assert "[DONE]" not in result.output
    assert [b["name"] for b in result.handoff_data["pending_blocks"]] == ["A", "B"]


def test_pending_blocks_follow_actual_failures() -> None:
    plan3 = json.dumps([{"name": n, "description": "d", "success_criteria": "c"} for n in "ABC"])

    def fn(i: int, messages: list[dict]) -> str:
        system = _system(messages)
        if "project planner" in system:
            return plan3
        if system.startswith("EVAL-PROMPT"):
            user = str(messages[-1].get("content"))
            return _verdict("## Block: A" not in user)  # only A is rejected
        return "done"

    result = LongRunRunner(ScriptedLLM(fn)).run(_ctx(evaluator_prompt="EVAL-PROMPT"))

    assert [b["name"] for b in result.handoff_data["pending_blocks"]] == ["A"]
    assert result.handoff_data["blocks_completed"] == 2


def test_planner_accepts_llm_response() -> None:
    def fn(i: int, messages: list[dict]):
        if "project planner" in _system(messages):
            return LLMResponse(content=PLAN, reasoning_content="thinking...")
        return "done"

    result = LongRunRunner(ScriptedLLM(fn)).run(_ctx())

    assert result.success is True
    assert result.plan.total_blocks == 2


def test_run_accepts_llm_config() -> None:
    seen: list = []

    def fn(i: int, messages: list[dict]) -> str:
        return PLAN if "project planner" in _system(messages) else "done"

    llm = ScriptedLLM(fn)
    result = LongRunRunner(llm).run(_ctx(), llm_config={"mode": "high"})
    seen = [c["config"] for c in llm.calls]

    assert result.success is True
    assert all(cfg and cfg.get("mode") == "high" for cfg in seen)


def test_preflight_uses_registered_tool_health_check(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("unused"))

    def handler(params: dict) -> str:
        return "ok"

    def broken() -> None:
        raise RuntimeError("service down")

    handler.health_check = broken  # type: ignore[attr-defined]
    from symbiote.environment.descriptors import ToolDescriptor

    kernel.tool_gateway.register_descriptor(
        ToolDescriptor(tool_id="svc", name="svc", description="d"), handler
    )
    runner = LongRunRunner(ScriptedLLM(PLAN), tool_gateway=kernel.tool_gateway)

    result = runner.run(_ctx(available_tools=[{"tool_id": "svc", "name": "svc"}]))

    assert result.success is False
    assert "service down" in result.error
    kernel.shutdown()


def test_compaction_strategy_carries_previous_block_output() -> None:
    def fn(i: int, messages: list[dict]) -> str:
        if "project planner" in _system(messages):
            return PLAN
        prompt = str(messages[-1].get("content"))
        return "OUTPUT-OF-A" if "## Current Block: A" in prompt else "done"

    llm = ScriptedLLM(fn)
    LongRunRunner(llm).run(_ctx(context_strategy="compaction"))
    block_b = next(c for c in llm.calls if "## Current Block: B" in str(c["messages"][-1]["content"]))
    assert "OUTPUT-OF-A" in str(block_b["messages"][-1]["content"])

    llm = ScriptedLLM(fn)
    LongRunRunner(llm).run(_ctx(context_strategy="reset"))
    block_b = next(c for c in llm.calls if "## Current Block: B" in str(c["messages"][-1]["content"]))
    assert "OUTPUT-OF-A" not in str(block_b["messages"][-1]["content"])


# ── The reply the user reads ────────────────────────────────────────────────


PLAN3 = json.dumps([{"name": n, "description": "d", "success_criteria": "c"} for n in "ABC"])


def test_plan_beyond_the_limit_is_cut_before_anyone_sees_it() -> None:
    def fn(i: int, messages: list[dict]) -> str:
        system = _system(messages)
        if "project planner" in system:
            return PLAN3  # ignores "at most 2 blocks"
        if system.startswith("You ran a multi-step project"):
            return "resposta"
        return f"relatório do bloco {i}"

    llm = ScriptedLLM(fn)
    published: list = []
    progress: list = []
    runner = LongRunRunner(
        llm, on_plan=published.append, on_progress=lambda e, i, n: progress.append((e, i, n))
    )
    result = runner.run(_ctx(max_blocks=2, persona={"role": "Sol", "language": "português do Brasil"}))

    assert [b["name"] for b in published[0].blocks] == ["A", "B"]
    assert {n for _e, _i, n in progress} == {2}
    assert result.loop_trace.stop_reason == "end_turn"
    assert result.output == "resposta"
    assert "2/2 blocks" in result.handoff_data["output_summary"]
    # The answer is written from the reports, in the persona's voice, and
    # knows what was never run.
    answer_call = llm.calls[-1]["messages"]
    assert "Sol" in answer_call[0]["content"]
    assert "relatório do bloco" in answer_call[1]["content"]
    assert "## Not run\n- C" in answer_call[1]["content"]


def test_a_failed_answer_call_falls_back_to_the_last_report() -> None:
    def fn(i: int, messages: list[dict]) -> str:
        system = _system(messages)
        if "project planner" in system:
            return PLAN
        if system.startswith("You ran a multi-step project"):
            raise ConnectionError("gateway down")
        return f"relatório {i}"

    result = LongRunRunner(ScriptedLLM(fn)).run(_ctx())

    assert result.success
    assert result.output.startswith("relatório")
    assert "blocks" not in result.output


def test_no_answer_call_when_no_block_succeeded() -> None:
    def fn(i: int, messages: list[dict]) -> str:
        system = _system(messages)
        if "project planner" in system:
            return PLAN
        if system.startswith("EVAL-PROMPT"):
            return _verdict(False)
        return "claims done"

    llm = ScriptedLLM(fn)
    result = LongRunRunner(llm).run(_ctx(evaluator_prompt="EVAL-PROMPT"))

    assert result.success is False
    assert not any(_system(c["messages"]).startswith("You ran") for c in llm.calls)
