"""Regression: spawn depth follows the actual delegation chain, and a failed
sub-run closes its session (report item E-12)."""

from __future__ import annotations

import time
from pathlib import Path

from tests.support import ScriptedLLM, make_kernel, tool_call_block


def _outcomes(out) -> list:
    return [r["output"]["success"] or r["output"]["error"] for r in out["tool_results"]]


def _fn(i: int, messages: list[dict]) -> str:
    first = str(messages[1].get("content"))
    last = str(messages[-1].get("content"))
    if "[Tool result: spawn]" in last or "[Tool error: spawn]" in last:
        return "ok"
    if "SUBTASK" in first:
        if "FAIL" in first:
            raise RuntimeError("401 unauthorized")
        if "CHAIN" in first:
            # each worker delegates again: real nesting
            return tool_call_block("spawn", target_symbiote="worker", task="SUBTASK CHAIN")
        time.sleep(0.2)
        return "sub-result"
    if "FANOUT" in first:
        return "\n".join(
            tool_call_block("spawn", target_symbiote="worker", task=f"SUBTASK {j}") for j in range(5)
        )
    if "DELEGATE_FAIL" in first:
        return tool_call_block("spawn", target_symbiote="worker", task="SUBTASK FAIL")
    if "DEEP" in first:
        return tool_call_block("spawn", target_symbiote="worker", task="SUBTASK CHAIN")
    return "hello"


def _kernel(tmp_path: Path):
    kernel = make_kernel(tmp_path, ScriptedLLM(_fn))
    boss = kernel.create_symbiote(name="boss", role="assistant")
    worker = kernel.create_symbiote(name="worker", role="assistant")
    kernel.environment.configure(symbiote_id=boss.id, tools=["spawn"])
    kernel.environment.configure(symbiote_id=worker.id, tools=["spawn"])
    return kernel, boss


def test_parallel_spawns_at_depth_one_all_run(tmp_path: Path) -> None:
    kernel, boss = _kernel(tmp_path)
    session = kernel.start_session(boss.id)

    out = kernel.message(session.id, "FANOUT")

    assert _outcomes(out) == [True] * 5
    kernel.shutdown()


def test_real_nesting_is_capped(tmp_path: Path) -> None:
    kernel, boss = _kernel(tmp_path)
    session = kernel.start_session(boss.id)

    kernel.message(session.id, "DEEP")

    sub_sessions = kernel._storage.fetch_all(
        "SELECT id FROM sessions WHERE goal LIKE '[subagent]%'"
    )
    assert len(sub_sessions) == kernel._subagent.MAX_DEPTH
    kernel.shutdown()


def test_failed_sub_run_closes_its_session(tmp_path: Path) -> None:
    kernel, boss = _kernel(tmp_path)
    session = kernel.start_session(boss.id)

    out = kernel.message(session.id, "DELEGATE_FAIL")

    assert _outcomes(out)[0] != True  # noqa: E712
    rows = kernel._storage.fetch_all(
        "SELECT status FROM sessions WHERE goal LIKE '[subagent] SUBTASK FAIL%'"
    )
    assert [r["status"] for r in rows] == ["closed"]
    kernel.shutdown()
