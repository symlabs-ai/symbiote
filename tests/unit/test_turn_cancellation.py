"""Cancelling a turn (kernel.cancel_turn).

Acceptance: after the request no new tool call and no new model call
start; what is running finishes; the final state tells a cancelled turn
from a completed one; the user message and the partial trace are kept.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.config.models import KernelConfig
from symbiote.core.exceptions import TurnCancelledError
from symbiote.core.kernel import SymbioteKernel
from symbiote.core.session_lock import StorageSessionLock
from tests.support import ScriptedLLM, add_tool, make_kernel, tool_call_block


class Scene:
    """A kernel, one session and a model that asks for a tool, then answers."""

    def __init__(self, tmp_path: Path, **config: Any) -> None:
        self.calls: list[str] = []
        self.in_tool = threading.Event()
        self.in_model = threading.Event()
        self.release = threading.Event()
        self.block_model_call: int | None = None  # index of the model call that waits
        self._model_calls = 0  # model calls of the turn in progress

        def model(_index: int, _messages: list[dict]) -> str:
            i = self._model_calls
            self._model_calls += 1
            self.calls.append(f"model-{i}")
            if self.block_model_call == i:
                self.in_model.set()
                self.release.wait(10)
            return tool_call_block("work", step=i) if i < 2 else "final answer"

        self.kernel = make_kernel(tmp_path, ScriptedLLM(model), **config)
        sym = self.kernel.create_symbiote(name="a", role="r")
        self.symbiote = sym.id
        self.session = self.kernel.start_session(sym.id).id
        self.block_tool = True

        def work(params: dict) -> str:
            self.calls.append(f"tool-{params.get('step')}")
            if self.block_tool:
                self.in_tool.set()
                self.release.wait(10)
            self.calls.append(f"tool-{params.get('step')}-finished")
            return "worked"

        add_tool(self.kernel, sym.id, "work", work)

    def message(self, text: str = "do it") -> Any:
        self._model_calls = 0
        return self.kernel.message(self.session, text)

    def run(self, text: str = "do it") -> tuple[threading.Thread, list[Any]]:
        outcome: list[Any] = []
        self._model_calls = 0

        def target() -> None:
            try:
                outcome.append(self.kernel.message(self.session, text))
            except BaseException as exc:  # noqa: BLE001
                outcome.append(exc)

        thread = threading.Thread(target=target)
        thread.start()
        return thread, outcome

    def messages(self) -> list[tuple[str, str]]:
        rows = self.kernel._storage.fetch_all(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY created_at, rowid",
            (self.session,),
        )
        return [(r["role"], r["content"]) for r in rows]

    def traces(self) -> list[dict]:
        return list(reversed(self.kernel.inspector.list_traces(session_id=self.session)))


def _steps(trace: dict) -> list[str]:
    return [step["tool_id"] for step in trace["steps"]]


# ── the acceptance criterion ────────────────────────────────────────────


def test_cancel_while_a_tool_runs(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    thread, outcome = scene.run()
    assert scene.in_tool.wait(10)

    assert scene.kernel.cancel_turn(scene.session) is True
    time.sleep(0.05)
    assert thread.is_alive()  # the running tool is not interrupted
    scene.release.set()
    thread.join(10)

    # The tool that was running finished; nothing else started.
    assert scene.calls == ["model-0", "tool-0", "tool-0-finished"]
    assert isinstance(outcome[0], TurnCancelledError)
    assert outcome[0].session_id == scene.session
    # The user message is kept; no assistant message was stored.
    assert scene.messages() == [("user", "do it")]
    # The partial trace is stored, and says the turn was cancelled.
    [trace] = scene.traces()
    assert trace["stop_reason"] == "cancelled"
    assert _steps(trace) == ["work"]
    assert trace["steps"][0]["success"] is True
    scene.kernel.shutdown()


def test_cancel_while_the_model_answers(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    scene.block_tool = False
    scene.block_model_call = 1  # the first tool round completes, then the model is slow
    thread, outcome = scene.run()
    assert scene.in_model.wait(10)

    assert scene.kernel.cancel_turn(scene.session) is True
    scene.release.set()
    thread.join(10)

    # The model call that was running finished; the tool it asked for never ran.
    assert scene.calls == ["model-0", "tool-0", "tool-0-finished", "model-1"]
    assert isinstance(outcome[0], TurnCancelledError)
    [trace] = scene.traces()
    assert trace["stop_reason"] == "cancelled" and _steps(trace) == ["work"]
    scene.kernel.shutdown()


def test_cancel_before_any_tool_leaves_an_empty_cancelled_trace(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    scene.block_model_call = 0
    thread, outcome = scene.run()
    assert scene.in_model.wait(10)
    scene.kernel.cancel_turn(scene.session)
    scene.release.set()
    thread.join(10)

    assert scene.calls == ["model-0"]
    assert isinstance(outcome[0], TurnCancelledError)
    assert scene.messages() == [("user", "do it")]
    [trace] = scene.traces()
    assert trace["stop_reason"] == "cancelled" and trace["steps"] == []
    scene.kernel.shutdown()


def test_a_completed_turn_is_told_apart_from_a_cancelled_one(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    scene.block_tool = False
    reply = scene.message("do it")
    assert reply["text"] == "final answer"

    scene.block_tool = True
    scene.calls.clear()
    thread, outcome = scene.run("again")
    assert scene.in_tool.wait(10)
    scene.kernel.cancel_turn(scene.session)
    scene.release.set()
    thread.join(10)

    completed, cancelled = scene.traces()
    assert completed["stop_reason"] == "end_turn"
    assert cancelled["stop_reason"] == "cancelled"
    assert [role for role, _ in scene.messages()] == ["user", "assistant", "user"]
    scene.kernel.shutdown()


def test_async_turns_are_cancelled_the_same_way(tmp_path: Path) -> None:
    scene = Scene(tmp_path)

    async def run() -> BaseException | None:
        task = asyncio.ensure_future(scene.kernel.message_async(scene.session, "do it"))
        assert await asyncio.to_thread(scene.in_tool.wait, 10)
        assert scene.kernel.cancel_turn(scene.session) is True
        scene.release.set()
        try:
            await task
        except TurnCancelledError as exc:
            return exc
        return None

    assert isinstance(asyncio.run(run()), TurnCancelledError)
    assert scene.calls == ["model-0", "tool-0", "tool-0-finished"]
    assert scene.messages() == [("user", "do it")]
    [trace] = scene.traces()
    assert trace["stop_reason"] == "cancelled" and _steps(trace) == ["work"]
    scene.kernel.shutdown()


# ── around the turn ─────────────────────────────────────────────────────


def test_cancel_without_a_running_turn(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    assert scene.kernel.cancel_turn(scene.session) is False
    assert scene.kernel.cancel_turn("no-such-session") is False
    # A request made when nothing runs does not cancel the next turn.
    scene.block_tool = False
    assert scene.message("do it")["text"] == "final answer"
    scene.kernel.shutdown()


def test_only_the_named_session_is_cancelled(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    other = scene.kernel.start_session(scene.symbiote).id
    thread, outcome = scene.run()
    assert scene.in_tool.wait(10)

    assert scene.kernel.cancel_turn(other) is False
    scene.release.set()
    thread.join(10)

    assert outcome[0]["text"] == "final answer"
    scene.kernel.shutdown()


def test_the_session_keeps_working_after_a_cancelled_turn(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    thread, _outcome = scene.run("first request")
    assert scene.in_tool.wait(10)
    scene.kernel.cancel_turn(scene.session)
    scene.release.set()
    thread.join(10)

    scene.block_tool = False
    assert scene.message("second request")["text"] == "final answer"

    # The cancelled request is still in the session, before the new exchange.
    assert [content for _role, content in scene.messages()][:2] == ["first request", "second request"]
    assert [role for role, _ in scene.messages()] == ["user", "user", "assistant"]
    scene.kernel.shutdown()


def test_a_cancelled_turn_is_not_a_failure_to_learn_from(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    thread, _outcome = scene.run()
    assert scene.in_tool.wait(10)
    scene.kernel.cancel_turn(scene.session)
    scene.release.set()
    thread.join(10)

    scene.kernel.close_session(scene.session)

    failures = scene.kernel._storage.fetch_all(
        "SELECT content FROM memory_entries WHERE symbiote_id = ? AND type = 'procedural'",
        (scene.symbiote,),
    )
    assert failures == []
    scene.kernel.shutdown()


def test_instant_mode(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    scene.kernel.environment.configure(symbiote_id=scene.symbiote, tool_mode="instant")
    scene.block_model_call = 0
    thread, outcome = scene.run()
    assert scene.in_model.wait(10)
    scene.kernel.cancel_turn(scene.session)
    scene.release.set()
    thread.join(10)

    assert scene.calls == ["model-0"]  # the tool it asked for never ran
    assert isinstance(outcome[0], TurnCancelledError)
    assert scene.messages() == [("user", "do it")]
    [trace] = scene.traces()
    assert trace["stop_reason"] == "cancelled" and trace["tool_mode"] == "instant"
    scene.kernel.shutdown()


def test_long_run_mode(tmp_path: Path) -> None:
    calls: list[str] = []
    in_block = threading.Event()
    release = threading.Event()

    def model(i: int, messages: list[dict]) -> str:
        system = str(messages[0].get("content", ""))
        if "PLAN" in system:
            calls.append("planner")
            return '[{"name": "one", "description": "d"}, {"name": "two", "description": "d"}]'
        calls.append(f"block-call-{i}")
        in_block.set()
        release.wait(10)
        return "block done"

    kernel = make_kernel(tmp_path, ScriptedLLM(model))
    sym = kernel.create_symbiote(name="a", role="r")
    kernel.environment.configure(
        symbiote_id=sym.id, tool_mode="long_run", planner_prompt="PLAN the work as JSON blocks."
    )
    session = kernel.start_session(sym.id).id
    outcome: list[Any] = []

    def target() -> None:
        try:
            outcome.append(kernel.message(session, "build it"))
        except BaseException as exc:  # noqa: BLE001
            outcome.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    assert in_block.wait(10)
    assert kernel.cancel_turn(session) is True
    release.set()
    thread.join(10)

    # The first block's model call finished; the second block never started.
    assert calls == ["planner", "block-call-1"]
    assert isinstance(outcome[0], TurnCancelledError)
    [trace] = kernel.inspector.list_traces(session_id=session)
    assert trace["stop_reason"] == "cancelled" and trace["tool_mode"] == "long_run"
    rows = kernel._storage.fetch_all("SELECT role FROM messages WHERE session_id = ?", (session,))
    assert [r["role"] for r in rows] == ["user"]
    kernel.shutdown()


# ── across replicas ─────────────────────────────────────────────────────


def _replica(tmp_path: Path, name: str, llm: Any, *, shared_lock: bool) -> SymbioteKernel:
    storage = SQLiteAdapter(tmp_path / "shared.db", check_same_thread=False)
    return SymbioteKernel(
        KernelConfig(
            db_path=tmp_path / f"unused-{name}.db",
            skills_root=tmp_path / name,
            turn_cancel_poll_interval=0.05,
        ),
        llm=llm,
        storage=storage,
        session_lock=StorageSessionLock(storage) if shared_lock else None,
    )


def _slow_tool_scene(tmp_path: Path, *, shared_lock: bool):
    calls: list[str] = []
    in_tool = threading.Event()

    def model(i: int, _m: list[dict]) -> str:
        calls.append(f"model-{i}")
        return tool_call_block("work") if i == 0 else "final answer"

    def work(_params: dict) -> str:
        calls.append("tool")
        in_tool.set()
        time.sleep(0.6)
        return "worked"

    runs_it = _replica(tmp_path, "one", ScriptedLLM(model), shared_lock=shared_lock)
    other = _replica(tmp_path, "two", ScriptedLLM("unused"), shared_lock=shared_lock)
    sym = runs_it.create_symbiote(name="a", role="r")
    add_tool(runs_it, sym.id, "work", work)
    session = runs_it.start_session(sym.id).id
    return runs_it, other, session, calls, in_tool


def test_a_cancel_on_another_replica_reaches_the_turn(tmp_path: Path) -> None:
    runs_it, other, session, calls, in_tool = _slow_tool_scene(tmp_path, shared_lock=True)
    outcome: list[Any] = []

    def target() -> None:
        try:
            outcome.append(runs_it.message(session, "do it"))
        except BaseException as exc:  # noqa: BLE001
            outcome.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    assert in_tool.wait(10)

    assert other.cancel_turn(session) is True  # relayed through the lock row
    thread.join(10)

    assert calls == ["model-0", "tool"]
    assert isinstance(outcome[0], TurnCancelledError)
    [trace] = runs_it.inspector.list_traces(session_id=session)
    assert trace["stop_reason"] == "cancelled"
    # The next turn on that session is not affected by the old request.
    assert other.cancel_turn(session) is False
    for kernel in (runs_it, other):
        kernel.shutdown()


def test_without_a_relaying_lock_the_other_replica_cannot_cancel(tmp_path: Path) -> None:
    runs_it, other, session, calls, in_tool = _slow_tool_scene(tmp_path, shared_lock=False)
    outcome: list[Any] = []
    thread = threading.Thread(target=lambda: outcome.append(runs_it.message(session, "do it")))
    thread.start()
    assert in_tool.wait(10)

    assert other.cancel_turn(session) is False
    thread.join(10)

    assert outcome[0]["text"] == "final answer"
    for kernel in (runs_it, other):
        kernel.shutdown()


def test_cancel_reaches_the_api_as_409(tmp_path: Path) -> None:
    from symbiote.api.http import app  # the handler is registered on import

    handler = app.exception_handlers[TurnCancelledError]
    response = asyncio.run(handler(None, TurnCancelledError("s1")))  # type: ignore[arg-type]
    assert response.status_code == 409
    with pytest.raises(TurnCancelledError, match="was cancelled"):
        raise TurnCancelledError("s1")
