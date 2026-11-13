"""Regression: async tool handlers run on every execution path and their real
result reaches the model (report item E-5)."""

from __future__ import annotations

import asyncio
import contextvars
from pathlib import Path

import pytest

from tests.support import ScriptedLLM, add_tool, make_kernel, tool_call_block

MARK = "ORDER-42"


async def create_order(params: dict) -> dict:
    await asyncio.sleep(0.01)
    return {"order_id": MARK}


def _script(i: int, messages: list[dict]) -> str:
    seen = "\n".join(str(m.get("content")) for m in messages[1:])
    if MARK in seen:
        return f"Created {MARK}"
    if i >= 2:
        return "Done."
    return tool_call_block("create_order", item="x")


def _text(out) -> str:
    return out["text"] if isinstance(out, dict) else out


def _kernel(tmp_path: Path, mode: str):
    llm = ScriptedLLM(_script)
    kernel = make_kernel(tmp_path, llm)
    sym = kernel.create_symbiote(name="a", role="assistant")
    add_tool(kernel, sym.id, "create_order", create_order, tool_mode=mode)
    return kernel, llm, kernel.start_session(sym.id)


@pytest.mark.parametrize("mode", ["brief", "instant"])
def test_sync_message_runs_async_handler(tmp_path: Path, mode: str) -> None:
    kernel, llm, session = _kernel(tmp_path, mode)

    out = kernel.message(session.id, "create it")

    assert "<coroutine" not in llm.all_prompts_text()
    if mode == "brief":
        assert MARK in _text(out)
    tool_out = out["tool_results"][0]["output"] if isinstance(out, dict) else None
    assert tool_out == {"order_id": MARK} or MARK in str(tool_out)
    kernel.shutdown()


@pytest.mark.parametrize("mode", ["brief", "instant"])
def test_async_message_runs_async_handler(tmp_path: Path, mode: str) -> None:
    kernel, llm, session = _kernel(tmp_path, mode)

    out = asyncio.run(kernel.message_async(session.id, "create it"))

    assert "<coroutine" not in llm.all_prompts_text()
    assert "<coroutine" not in str(out)
    assert MARK in str(out)
    kernel.shutdown()


def test_gateway_execute_returns_real_output(tmp_path: Path) -> None:
    kernel, _llm, session = _kernel(tmp_path, "brief")
    sym_id = kernel.get_session(session.id).symbiote_id

    result = kernel.tool_gateway.execute(sym_id, session.id, "create_order", {})

    assert result.success
    assert result.output == {"order_id": MARK}
    kernel.shutdown()


def test_consecutive_sync_calls_share_one_event_loop(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sym = kernel.create_symbiote(name="a", role="assistant")
    state: dict = {}

    async def first(params: dict) -> str:
        # An object bound to the running loop, as a browser session would be.
        state["future"] = asyncio.get_running_loop().create_future()
        return "created"

    async def second(params: dict) -> str:
        fut = state["future"]
        fut.set_result("reused")
        return await fut

    add_tool(kernel, sym.id, "first", first)
    add_tool(kernel, sym.id, "second", second)

    assert kernel.tool_gateway.execute(sym.id, None, "first", {}).output == "created"
    result = kernel.tool_gateway.execute(sym.id, None, "second", {})
    assert result.success, result.error
    assert result.output == "reused"
    kernel.shutdown()


def test_async_handler_sees_caller_contextvars(tmp_path: Path) -> None:
    var: contextvars.ContextVar[str] = contextvars.ContextVar("host_user", default="none")
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sym = kernel.create_symbiote(name="a", role="assistant")

    async def whoami(params: dict) -> str:
        return var.get()

    add_tool(kernel, sym.id, "whoami", whoami)
    var.set("alice")

    assert kernel.tool_gateway.execute(sym.id, None, "whoami", {}).output == "alice"
    kernel.shutdown()


def test_shutdown_stops_the_tool_loop(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sym = kernel.create_symbiote(name="a", role="assistant")
    add_tool(kernel, sym.id, "create_order", create_order)
    kernel.tool_gateway.execute(sym.id, None, "create_order", {})
    thread = kernel.tool_gateway._async_thread
    assert thread is not None and thread.is_alive()

    kernel.shutdown()

    thread.join(timeout=5)
    assert not thread.is_alive()
