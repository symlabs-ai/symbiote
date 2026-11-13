"""Regression: message_async does not run the synchronous LLM call on the
event loop thread (report item E-9)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from tests.support import add_tool, make_kernel


class SlowLLM:
    def __init__(self, delay: float, fail_first: bool = False) -> None:
        self.delay = delay
        self.fail_first = fail_first
        self.calls = 0

    def complete(self, messages, config=None, tools=None):
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise ConnectionError("503 upstream")
        time.sleep(self.delay)
        return "ok"

    def stream(self, messages, config=None, tools=None):
        yield self.complete(messages, config, tools)


async def _run_with_heartbeat(coros):
    gaps: list[float] = []
    stop = asyncio.Event()

    async def heartbeat() -> None:
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    hb = asyncio.create_task(heartbeat())
    start = time.monotonic()
    results = await asyncio.gather(*coros)
    elapsed = time.monotonic() - start
    stop.set()
    await hb
    return results, elapsed, max(gaps)


@pytest.mark.parametrize("mode", ["brief", "instant"])
def test_sessions_run_concurrently_without_freezing_the_loop(tmp_path: Path, mode: str) -> None:
    kernel = make_kernel(tmp_path, SlowLLM(0.3))
    sym = kernel.create_symbiote(name="a", role="assistant")
    add_tool(kernel, sym.id, "noop", lambda p: "ok", tool_mode=mode)
    sessions = [kernel.start_session(sym.id) for _ in range(4)]

    results, elapsed, worst_gap = asyncio.run(
        _run_with_heartbeat([kernel.message_async(s.id, "hi") for s in sessions])
    )

    assert results == ["ok"] * 4
    assert elapsed < 0.8
    assert worst_gap < 0.1
    kernel.shutdown()


def test_tokens_still_reach_on_token_in_order(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, SlowLLM(0.05))
    sym = kernel.create_symbiote(name="a", role="assistant")
    session = kernel.start_session(sym.id)
    seen: list[str] = []

    out = asyncio.run(kernel.message_async(session.id, "hi", on_token=seen.append))

    assert out == "ok"
    assert "".join(seen) == "ok"
    kernel.shutdown()
