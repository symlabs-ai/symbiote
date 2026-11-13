"""Regression: loop traces and scores belong to the session that produced
them (report item E-3)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from symbiote.core.capabilities import CapabilityError
from tests.support import ScriptedLLM, add_tool, make_kernel, tool_call_block


def _score(kernel, session_id: str) -> dict:
    return kernel._storage.fetch_one(
        "SELECT * FROM session_scores WHERE session_id = ?", (session_id,)
    )


def _failure_memories(kernel, symbiote_id: str) -> list:
    return kernel._memory.search("", tags=["harness_failure"], symbiote_id=symbiote_id)


def test_other_session_does_not_overwrite_trace_before_close(tmp_path: Path) -> None:
    def fn(i: int, messages: list[dict]) -> str:
        user = str(messages[-1].get("content"))
        if "STUCK" in user or "[Tool result: echo]" in user:
            # Session A: the same call twice in a row -> stagnation
            return tool_call_block("echo", x=1)
        if "[Tool result: lookup]" in user:
            return "found it"
        # Session B: one successful tool call -> a healthy end_turn trace
        return tool_call_block("lookup", q="x")

    kernel = make_kernel(tmp_path, ScriptedLLM(fn))
    sym = kernel.create_symbiote(name="a", role="assistant")
    add_tool(kernel, sym.id, "echo", lambda p: "same")
    add_tool(kernel, sym.id, "lookup", lambda p: "result")
    session_a = kernel.start_session(sym.id)
    session_b = kernel.start_session(sym.id)

    kernel.message(session_a.id, "STUCK")
    kernel.message(session_b.id, "hello")  # healthy tool use, other session
    kernel.close_session(session_a.id)

    score = _score(kernel, session_a.id)
    assert score["stop_reason"] == "stagnation"
    assert score["auto_score"] < 0.5
    assert any("estagnou" in m.content for m in _failure_memories(kernel, sym.id))
    kernel.shutdown()


def test_concurrent_sessions_keep_their_own_traces(tmp_path: Path) -> None:
    def fn(i: int, messages: list[dict]) -> str:
        user = str(messages[-1].get("content"))
        if "[Tool result" in user:
            return "done"
        if "USE-TOOL " in user:
            marker = user.split("USE-TOOL ", 1)[1].split()[0]
            return tool_call_block("echo", marker=marker)
        return "no tools here"

    kernel = make_kernel(tmp_path, ScriptedLLM(fn))
    sym = kernel.create_symbiote(name="a", role="assistant")
    add_tool(kernel, sym.id, "echo", lambda p: p)
    sessions = [kernel.start_session(sym.id) for _ in range(8)]
    rounds = 15
    barrier = threading.Barrier(len(sessions))
    errors: list[BaseException] = []

    def worker(idx: int) -> None:
        sess = sessions[idx]
        try:
            barrier.wait()
            for _ in range(rounds):
                text = f"USE-TOOL {sess.id}" if idx % 2 == 0 else "hi"
                kernel.message(sess.id, text)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(sessions))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert errors == []
    rows = kernel._storage.fetch_all("SELECT session_id, steps_json FROM execution_traces")
    tool_sessions = {s.id for i, s in enumerate(sessions) if i % 2 == 0}
    assert len(rows) == len(tool_sessions) * rounds
    for row in rows:
        assert row["session_id"] in tool_sessions
        markers = {step["params"]["marker"] for step in json.loads(row["steps_json"])}
        assert markers == {row["session_id"]}
    kernel.shutdown()


def test_failed_run_still_persists_its_trace(tmp_path: Path) -> None:
    def fn(i: int, messages: list[dict]) -> str:
        if i == 0:
            return tool_call_block("echo", x=1)
        raise RuntimeError("400 invalid_request: context_length_exceeded")

    kernel = make_kernel(tmp_path, ScriptedLLM(fn))
    sym = kernel.create_symbiote(name="a", role="assistant")
    add_tool(kernel, sym.id, "echo", lambda p: "ok")
    session = kernel.start_session(sym.id)

    with pytest.raises(CapabilityError):
        kernel.message(session.id, "go")

    rows = kernel._storage.fetch_all(
        "SELECT total_tool_calls FROM execution_traces WHERE session_id = ?", (session.id,)
    )
    assert [r["total_tool_calls"] for r in rows] == [1]
    kernel.shutdown()


def test_kernel_has_no_single_trace_slot(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    assert not hasattr(kernel, "_last_trace")
    assert not hasattr(kernel, "_last_handoff")
    kernel.shutdown()
