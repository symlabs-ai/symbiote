"""Regression: hooks registered on kernel.hooks actually fire (report item E-2)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from symbiote.core.hooks import BaseHook
from tests.support import ScriptedLLM, add_tool, make_kernel, tool_call_block


class Recorder(BaseHook):
    def __init__(self) -> None:
        self.events: list[tuple] = []

    async def before_tool(self, tool_id, params) -> None:
        self.events.append(("before_tool", tool_id, dict(params)))

    async def after_tool(self, tool_id, params, result) -> None:
        self.events.append(("after_tool", tool_id, getattr(result, "success", None)))

    async def before_turn(self, messages) -> None:
        self.events.append(("before_turn", messages[-1]["content"]))

    async def after_turn(self, messages, response) -> None:
        self.events.append(("after_turn", response))


class Exploding(BaseHook):
    async def before_tool(self, tool_id, params) -> None:
        raise RuntimeError("hook bug")

    async def after_turn(self, messages, response) -> None:
        raise RuntimeError("hook bug")


def _kernel(tmp_path: Path):
    llm = ScriptedLLM(lambda i, m: tool_call_block("echo", x=1) if i % 2 == 0 else "done")
    kernel = make_kernel(tmp_path, llm)
    sym = kernel.create_symbiote(name="a", role="assistant")
    add_tool(kernel, sym.id, "echo", lambda p: "ok")
    return kernel, kernel.start_session(sym.id)


EXPECTED = [
    ("before_turn", "use the tool"),
    ("before_tool", "echo", {"x": 1}),
    ("after_tool", "echo", True),
    ("after_turn", "done"),
]


def test_sync_message_dispatches_hooks(tmp_path: Path) -> None:
    kernel, session = _kernel(tmp_path)
    rec = Recorder()
    kernel.hooks.add(rec)

    kernel.message(session.id, "use the tool")

    assert rec.events == EXPECTED
    kernel.shutdown()


def test_async_message_dispatches_hooks(tmp_path: Path) -> None:
    kernel, session = _kernel(tmp_path)
    rec = Recorder()
    kernel.hooks.add(rec)

    asyncio.run(kernel.message_async(session.id, "use the tool"))

    assert rec.events == EXPECTED
    kernel.shutdown()


def test_direct_gateway_execution_dispatches_tool_hooks(tmp_path: Path) -> None:
    kernel, session = _kernel(tmp_path)
    rec = Recorder()
    kernel.hooks.add(rec)
    sym_id = kernel.get_session(session.id).symbiote_id

    kernel.tool_gateway.execute(sym_id, session.id, "echo", {"x": 2})
    kernel.tool_gateway.execute(sym_id, session.id, "not_authorized", {})

    assert rec.events[:2] == [("before_tool", "echo", {"x": 2}), ("after_tool", "echo", True)]
    kernel.shutdown()


@pytest.mark.parametrize("entry", ["sync", "async"])
def test_failing_hook_does_not_break_the_turn(tmp_path: Path, entry: str) -> None:
    kernel, session = _kernel(tmp_path)
    kernel.hooks.add(Exploding())
    rec = Recorder()
    kernel.hooks.add(rec)

    if entry == "sync":
        out = kernel.message(session.id, "use the tool")
    else:
        out = asyncio.run(kernel.message_async(session.id, "use the tool"))

    assert out["text"] == "done"
    assert rec.events == EXPECTED
    kernel.shutdown()
