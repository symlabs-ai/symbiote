"""Regression: a tool timeout hands control back on time on every path
(report item E-4)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from symbiote.environment.descriptors import ToolCall
from tests.support import ScriptedLLM, add_tool, make_kernel


def _slow(seconds: float):
    def handler(params: dict) -> str:
        time.sleep(seconds)
        return "done"

    return handler


def _kernel(tmp_path: Path):
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sym = kernel.create_symbiote(name="a", role="assistant")
    return kernel, sym.id


def test_sync_timeout_returns_on_time(tmp_path: Path) -> None:
    kernel, sym = _kernel(tmp_path)
    add_tool(kernel, sym, "slow", _slow(2.0))

    start = time.monotonic()
    result = kernel.tool_gateway.execute(sym, None, "slow", {}, timeout=0.2)
    elapsed = time.monotonic() - start

    assert not result.success and "timed out" in result.error
    assert elapsed < 1.0
    kernel.shutdown()


def test_async_timeout_returns_on_time(tmp_path: Path) -> None:
    kernel, sym = _kernel(tmp_path)
    add_tool(kernel, sym, "slow", _slow(2.0))

    start = time.monotonic()
    result = asyncio.run(kernel.tool_gateway.execute_async(sym, None, "slow", {}, timeout=0.2))
    elapsed = time.monotonic() - start

    assert not result.success
    assert elapsed < 1.0
    kernel.shutdown()


def test_parallel_timeout_counts_from_submission(tmp_path: Path) -> None:
    kernel, sym = _kernel(tmp_path)
    for name in ("a", "b", "c"):
        add_tool(kernel, sym, name, _slow(0.9))
    calls = [ToolCall(tool_id=name, params={}) for name in ("a", "b", "c")]

    start = time.monotonic()
    results = kernel.tool_gateway.execute_tool_calls(sym, None, calls, timeout=1.5)
    elapsed = time.monotonic() - start

    assert [r.success for r in results] == [True, True, True]
    assert elapsed < 1.5
    kernel.shutdown()


def test_parallel_timeout_is_not_cumulative(tmp_path: Path) -> None:
    kernel, sym = _kernel(tmp_path)
    for name in ("a", "b", "c"):
        add_tool(kernel, sym, name, _slow(5.0))
    calls = [ToolCall(tool_id=name, params={}) for name in ("a", "b", "c")]

    start = time.monotonic()
    results = kernel.tool_gateway.execute_tool_calls(sym, None, calls, timeout=0.3)
    elapsed = time.monotonic() - start

    assert [r.success for r in results] == [False, False, False]
    assert elapsed < 0.6  # one shared deadline, not 3 x 0.3 s
    kernel.shutdown()
