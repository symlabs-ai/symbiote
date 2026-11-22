"""Whether the wait for an approval counts toward ``loop_timeout`` is a
KernelConfig setting, ``loop_timeout_counts_approval`` (B-112, SymCoder
request).

``loop_timeout`` is a wall-clock limit on the tool loop, and the approval
callback may wait for a person as long as the host lets it. Counted in, a few
slow approvals end a turn that did almost no work. With the setting off the
loop takes that wait off its clock; ``total_elapsed_ms`` stays wall-clock
time.

The clock of the loop is a fake one: a person "takes" 400 s by moving it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from symbiote.config.models import KernelConfig
from symbiote.runners import chat as chat_mod
from symbiote.runners.chat import ChatRunner
from tests.support import ScriptedLLM, add_tool, make_kernel, tool_call_block

ROUNDS = 3
LOOP_TIMEOUT = 300.0
PERSON_SECONDS = 400.0
PLAN = json.dumps([{"name": "A", "description": "d", "success_criteria": "c"}])


class Clock:
    """Stands in for ``time`` inside the chat runner."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(chat_mod, "time", fake)
    return fake


def _write_files(i: int, _messages: list[dict]) -> str:
    return tool_call_block("write_file", n=i) if i < ROUNDS else "done"


def _run(
    tmp_path: Path,
    clock: Clock,
    *,
    work_seconds: float = 0.0,
    use_async: bool = False,
    **config,
) -> tuple[ScriptedLLM, dict]:
    """A turn of ROUNDS tool rounds, each approved by a person who takes
    PERSON_SECONDS; returns the model and the turn's trace."""
    llm = ScriptedLLM(_write_files)
    kernel = make_kernel(tmp_path, llm, **config)

    def person(_tool_id: str, _params: dict, _risk: str) -> bool:
        clock.now += PERSON_SECONDS
        return True

    def write_file(_params: dict) -> str:
        clock.now += work_seconds
        return "written"

    try:
        symbiote = kernel.create_symbiote(name="a", role="assistant")
        add_tool(kernel, symbiote.id, "write_file", write_file, loop_timeout=LOOP_TIMEOUT)
        kernel.set_approval_callback(person, risk_threshold="low")
        session = kernel.start_session(symbiote.id)
        if use_async:
            asyncio.run(kernel.message_async(session.id, "change the api"))
        else:
            kernel.message(session.id, "change the api")
        trace = kernel._storage.fetch_one(
            "SELECT * FROM execution_traces WHERE session_id = ?", (session.id,)
        )
    finally:
        kernel.shutdown()
    return llm, trace


class TestTheDefaultIsUnchanged:
    def test_the_wait_counts_by_default(self, tmp_path: Path) -> None:
        assert KernelConfig().loop_timeout_counts_approval is True
        kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
        try:
            chat = kernel._runner_registry.get("chat")
            long_run = kernel._runner_registry.get("long_run")
            assert chat._loop_timeout_counts_approval is True
            assert long_run._loop_timeout_counts_approval is True
        finally:
            kernel.shutdown()
        assert ChatRunner(ScriptedLLM("ok"))._loop_timeout_counts_approval is True

    @pytest.mark.parametrize("use_async", [False, True])
    def test_one_slow_approval_ends_the_turn(
        self, tmp_path: Path, clock: Clock, use_async: bool
    ) -> None:
        llm, trace = _run(tmp_path, clock, use_async=use_async)

        assert trace["stop_reason"] == "timeout"
        assert len(llm.calls) == 1


class TestTheWaitTakenOffTheClock:
    @pytest.mark.parametrize("use_async", [False, True])
    def test_slow_approvals_do_not_end_the_turn(
        self, tmp_path: Path, clock: Clock, use_async: bool
    ) -> None:
        llm, trace = _run(
            tmp_path, clock, use_async=use_async, loop_timeout_counts_approval=False
        )

        assert trace["stop_reason"] == "end_turn"
        assert len(llm.calls) == ROUNDS + 1
        # The trace still says how long the turn took on the wall clock.
        assert trace["total_elapsed_ms"] == int(ROUNDS * PERSON_SECONDS * 1000)

    def test_the_work_itself_still_times_out(self, tmp_path: Path, clock: Clock) -> None:
        llm, trace = _run(
            tmp_path, clock, work_seconds=200.0, loop_timeout_counts_approval=False
        )

        # 200 s of tool work per round: over the 300 s limit after two rounds,
        # whatever the 800 s the person took.
        assert trace["stop_reason"] == "timeout"
        assert len(llm.calls) == 2

    def test_a_turn_without_approvals_is_timed_as_before(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        llm = ScriptedLLM(_write_files)
        kernel = make_kernel(tmp_path, llm, loop_timeout_counts_approval=False)

        def write_file(_params: dict) -> str:
            clock.now += 200.0
            return "written"

        try:
            symbiote = kernel.create_symbiote(name="a", role="assistant")
            add_tool(kernel, symbiote.id, "write_file", write_file, loop_timeout=LOOP_TIMEOUT)
            session = kernel.start_session(symbiote.id)
            kernel.message(session.id, "change the api")
        finally:
            kernel.shutdown()

        assert len(llm.calls) == 2


def _long_run_block_calls(tmp_path: Path, clock: Clock, **config) -> int:
    """Run one long-run block of ROUNDS approved tool rounds; return how many
    model calls the block made."""
    block_calls = 0

    def fn(_i: int, messages: list[dict]) -> str:
        nonlocal block_calls
        system = str(messages[0].get("content"))
        if system.startswith("PLANNER-PROMPT"):
            return PLAN
        if system.startswith("You ran a multi-step project"):
            return "answer"
        block_calls += 1
        n = block_calls - 1
        return tool_call_block("write_file", n=n) if n < ROUNDS else "block done"

    def person(_tool_id: str, _params: dict, _risk: str) -> bool:
        clock.now += PERSON_SECONDS
        return True

    kernel = make_kernel(tmp_path, ScriptedLLM(fn), **config)
    try:
        symbiote = kernel.create_symbiote(name="a", role="assistant")
        add_tool(
            kernel,
            symbiote.id,
            "write_file",
            lambda _p: "written",
            tool_mode="long_run",
            planner_prompt="PLANNER-PROMPT: decompose",
            loop_timeout=LOOP_TIMEOUT,
        )
        kernel.set_approval_callback(person, risk_threshold="low")
        session = kernel.start_session(symbiote.id)
        kernel.message(session.id, "build it")
    finally:
        kernel.shutdown()
    return block_calls


class TestLongRunBlocks:
    def test_a_slow_approval_ends_the_block_by_default(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        assert _long_run_block_calls(tmp_path, clock) == 1

    def test_blocks_follow_the_setting(self, tmp_path: Path, clock: Clock) -> None:
        calls = _long_run_block_calls(tmp_path, clock, loop_timeout_counts_approval=False)

        assert calls == ROUNDS + 1
