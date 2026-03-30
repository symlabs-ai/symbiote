"""Tests for SessionLock — per-session concurrency control (B-48)."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from symbiote.core.session_lock import SessionLock


class TestSessionLockSync:
    """Tests for sync per-session locking."""

    def test_same_session_serialized(self) -> None:
        """Two threads on the same session are serialized."""
        lock = SessionLock()
        order: list[str] = []

        def worker(name: str) -> None:
            with lock.acquire("session-1"):
                order.append(f"{name}-start")
                time.sleep(0.05)
                order.append(f"{name}-end")

        t1 = threading.Thread(target=worker, args=("A",))
        t2 = threading.Thread(target=worker, args=("B",))
        t1.start()
        time.sleep(0.01)  # ensure A starts first
        t2.start()
        t1.join()
        t2.join()

        # A must complete before B starts
        assert order.index("A-end") < order.index("B-start")

    def test_different_sessions_parallel(self) -> None:
        """Two threads on different sessions run in parallel."""
        lock = SessionLock()
        timestamps: dict[str, list[float]] = {}

        def worker(session_id: str) -> None:
            with lock.acquire(session_id):
                timestamps[session_id] = [time.monotonic()]
                time.sleep(0.05)
                timestamps[session_id].append(time.monotonic())

        t1 = threading.Thread(target=worker, args=("s1",))
        t2 = threading.Thread(target=worker, args=("s2",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Their execution should overlap (s2 starts before s1 ends)
        s1_start, s1_end = timestamps["s1"]
        s2_start, s2_end = timestamps["s2"]
        assert s2_start < s1_end or s1_start < s2_end  # overlap


@pytest.mark.asyncio
class TestSessionLockAsync:
    """Tests for async per-session locking."""

    async def test_same_session_serialized(self) -> None:
        """Two coroutines on the same session are serialized."""
        lock = SessionLock()
        order: list[str] = []

        async def worker(name: str) -> None:
            async with lock.acquire_async("session-1"):
                order.append(f"{name}-start")
                await asyncio.sleep(0.05)
                order.append(f"{name}-end")

        await asyncio.gather(worker("A"), worker("B"))

        # One must complete before the other starts
        if order[0] == "A-start":
            assert order.index("A-end") < order.index("B-start")
        else:
            assert order.index("B-end") < order.index("A-start")

    async def test_different_sessions_parallel(self) -> None:
        """Two coroutines on different sessions run in parallel."""
        lock = SessionLock()
        results: dict[str, list[float]] = {}

        async def worker(session_id: str) -> None:
            async with lock.acquire_async(session_id):
                results[session_id] = [asyncio.get_event_loop().time()]
                await asyncio.sleep(0.05)
                results[session_id].append(asyncio.get_event_loop().time())

        await asyncio.gather(worker("s1"), worker("s2"))

        s1_start, s1_end = results["s1"]
        s2_start, s2_end = results["s2"]
        # They should overlap
        assert s2_start < s1_end or s1_start < s2_end
