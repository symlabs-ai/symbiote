"""``current_spawn()``: code running for a delegated symbiote knows it (B-84).

Hooks, the LLM adapter, the tool guard and tool handlers fire for the
delegated symbiote inside the turn of the one that called ``spawn``.
"""

from __future__ import annotations

from pathlib import Path

from symbiote.environment.exec_context import SpawnContext, current_spawn
from tests.support import ScriptedLLM, add_tool, make_kernel, tool_call_block


def _last(messages: list[dict]) -> str:
    return str(messages[-1]["content"])


def test_model_calls_of_the_delegated_symbiote_see_the_spawn(tmp_path: Path) -> None:
    seen: dict[str, SpawnContext | None] = {}

    def script(_i, messages):
        last = _last(messages)
        if "[Tool result: spawn]" in last:
            seen["boss_after"] = current_spawn()
            return "done"
        if "SUBTASK" in last:
            seen["worker"] = current_spawn()
            return "worker answer"
        seen["boss_before"] = current_spawn()
        return tool_call_block("spawn", target_symbiote="worker", task="SUBTASK")

    kernel = make_kernel(tmp_path, ScriptedLLM(script))
    try:
        boss = kernel.create_symbiote(name="boss", role="front desk")
        worker = kernel.create_symbiote(name="worker", role="orders")
        kernel.environment.configure(symbiote_id=boss.id, tools=["spawn"])
        session = kernel.start_session(boss.id)

        kernel.message(session.id, "go")

        assert seen["boss_before"] is None
        assert seen["boss_after"] is None
        spawn = seen["worker"]
        (sub_session,) = kernel.inspector.list_sessions(worker.id)
        assert spawn == SpawnContext(
            symbiote_id=worker.id,
            session_id=sub_session["id"],
            parent_symbiote_id=boss.id,
            parent_session_id=session.id,
            root_symbiote_id=boss.id,
            root_session_id=session.id,
            depth=1,
        )
        assert current_spawn() is None
    finally:
        kernel.shutdown()


def test_tools_and_guard_of_the_delegated_symbiote_see_the_spawn(tmp_path: Path) -> None:
    seen: dict[str, SpawnContext | None] = {}

    def script(_i, messages):
        last = _last(messages)
        if "[Tool result: spawn]" in last or "[Tool result: lookup]" in last:
            return "done"
        if "SUBTASK" in last:
            return tool_call_block("lookup")
        return tool_call_block("spawn", target_symbiote="worker", task="SUBTASK")

    def lookup(_params: dict) -> str:
        seen["tool"] = current_spawn()
        return "found"

    def guard(tool_id, _params, _context):
        from symbiote.environment.policies import ToolDecision

        seen[f"guard:{tool_id}"] = current_spawn()
        return ToolDecision.allow()

    kernel = make_kernel(tmp_path, ScriptedLLM(script))
    try:
        boss = kernel.create_symbiote(name="boss", role="front desk")
        worker = kernel.create_symbiote(name="worker", role="orders")
        kernel.environment.configure(symbiote_id=boss.id, tools=["spawn"])
        add_tool(kernel, worker.id, "lookup", lookup)
        kernel.set_tool_guard(guard)

        kernel.message(kernel.start_session(boss.id).id, "go")

        assert seen["guard:spawn"] is None
        assert seen["tool"] is not None
        assert seen["tool"].symbiote_id == worker.id
        assert seen["guard:lookup"] == seen["tool"]
    finally:
        kernel.shutdown()


def test_a_chain_keeps_its_root_and_counts_its_depth(tmp_path: Path) -> None:
    seen: dict[str, SpawnContext | None] = {}

    def script(_i, messages):
        last = _last(messages)
        if "[Tool result: spawn]" in last:
            return "done"
        if "LEAF" in last:
            seen["leaf"] = current_spawn()
            return "leaf answer"
        if "MIDDLE" in last:
            seen["middle"] = current_spawn()
            return tool_call_block("spawn", target_symbiote="leaf", task="LEAF")
        return tool_call_block("spawn", target_symbiote="middle", task="MIDDLE")

    kernel = make_kernel(tmp_path, ScriptedLLM(script))
    try:
        top = kernel.create_symbiote(name="top", role="a")
        middle = kernel.create_symbiote(name="middle", role="b")
        leaf = kernel.create_symbiote(name="leaf", role="c")
        for sym in (top, middle):
            kernel.environment.configure(symbiote_id=sym.id, tools=["spawn"])
        session = kernel.start_session(top.id)

        kernel.message(session.id, "go")

        assert seen["middle"].depth == 1
        assert seen["leaf"].depth == 2
        assert seen["leaf"].symbiote_id == leaf.id
        assert seen["leaf"].parent_symbiote_id == middle.id
        assert seen["leaf"].parent_session_id == seen["middle"].session_id
        assert seen["leaf"].root_symbiote_id == top.id
        assert seen["leaf"].root_session_id == session.id
    finally:
        kernel.shutdown()
