"""Tests for parallel tool execution in ToolGateway — B-55."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.identity import IdentityManager
from symbiote.environment.descriptors import ToolCall, ToolCallResult
from symbiote.environment.manager import EnvironmentManager
from symbiote.environment.policies import PolicyGate
from symbiote.environment.tools import ToolGateway

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "parallel_test.db"
    # check_same_thread=False needed because sync parallel uses ThreadPoolExecutor
    adp = SQLiteAdapter(db_path=db, check_same_thread=False)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name="ParallelBot", role="assistant")
    return sym.id


@pytest.fixture()
def env_manager(adapter: SQLiteAdapter) -> EnvironmentManager:
    return EnvironmentManager(storage=adapter)


@pytest.fixture()
def gate(env_manager: EnvironmentManager, adapter: SQLiteAdapter) -> PolicyGate:
    return PolicyGate(env_manager=env_manager, storage=adapter)


@pytest.fixture()
def gw(gate: PolicyGate) -> ToolGateway:
    return ToolGateway(policy_gate=gate)


# ── Helpers ──────────────────────────────────────────────────────────────────

TOOL_IDS = ["slow", "fail", "aslow", "afail"]


def _slow_handler(params: dict) -> str:
    """Sleeps for the given duration, returns a tag."""
    time.sleep(params.get("sleep", 0.1))
    return f"done-{params.get('tag', '?')}"


def _failing_handler(params: dict) -> str:
    raise RuntimeError("boom")


async def _async_slow_handler(params: dict) -> str:
    await asyncio.sleep(params.get("sleep", 0.1))
    return f"done-{params.get('tag', '?')}"


async def _async_failing_handler(params: dict) -> str:
    raise RuntimeError("boom")


def _configure_tools(gw: ToolGateway, env_manager: EnvironmentManager, symbiote_id: str) -> None:
    """Register tools and authorize them via policy."""
    gw.register_tool("slow", _slow_handler)
    gw.register_tool("fail", _failing_handler)
    gw.register_tool("aslow", _async_slow_handler)
    gw.register_tool("afail", _async_failing_handler)
    env_manager.configure(symbiote_id=symbiote_id, tools=TOOL_IDS)


# ── Sync: execute_tool_calls ─────────────────────────────────────────────────


class TestSyncParallel:
    def test_concurrent_execution(
        self, gw: ToolGateway, env_manager: EnvironmentManager, symbiote_id: str,
    ) -> None:
        """Multiple calls should run concurrently, completing faster than sequential."""
        _configure_tools(gw, env_manager, symbiote_id)

        calls = [
            ToolCall(tool_id="slow", params={"sleep": 0.15, "tag": str(i)})
            for i in range(4)
        ]
        start = time.monotonic()
        results = gw.execute_tool_calls(symbiote_id=symbiote_id, session_id=None, calls=calls)
        elapsed = time.monotonic() - start

        # Sequential would take ~0.6s; parallel with 4 workers should be ~0.15s
        assert elapsed < 0.5, f"Took {elapsed:.2f}s — expected parallel execution"
        assert len(results) == 4
        assert all(r.success for r in results)

    def test_one_failure_doesnt_block_others(
        self, gw: ToolGateway, env_manager: EnvironmentManager, symbiote_id: str,
    ) -> None:
        _configure_tools(gw, env_manager, symbiote_id)

        calls = [
            ToolCall(tool_id="slow", params={"sleep": 0.05, "tag": "a"}),
            ToolCall(tool_id="fail", params={}),
            ToolCall(tool_id="slow", params={"sleep": 0.05, "tag": "b"}),
        ]
        results = gw.execute_tool_calls(symbiote_id=symbiote_id, session_id=None, calls=calls)

        assert len(results) == 3
        assert results[0].success is True
        assert results[1].success is False
        assert "boom" in (results[1].error or "")
        assert "[Hint:" in (results[1].error or "")
        assert results[2].success is True

    def test_result_order_preserved(
        self, gw: ToolGateway, env_manager: EnvironmentManager, symbiote_id: str,
    ) -> None:
        _configure_tools(gw, env_manager, symbiote_id)

        calls = [
            ToolCall(tool_id="slow", params={"sleep": 0.1, "tag": "first"}),
            ToolCall(tool_id="slow", params={"sleep": 0.01, "tag": "second"}),
        ]
        results = gw.execute_tool_calls(symbiote_id=symbiote_id, session_id=None, calls=calls)

        assert results[0].output == "done-first"
        assert results[1].output == "done-second"

    def test_single_call(
        self, gw: ToolGateway, env_manager: EnvironmentManager, symbiote_id: str,
    ) -> None:
        _configure_tools(gw, env_manager, symbiote_id)
        calls = [ToolCall(tool_id="slow", params={"tag": "only"})]
        results = gw.execute_tool_calls(symbiote_id=symbiote_id, session_id=None, calls=calls)

        assert len(results) == 1
        assert results[0].success is True
        assert results[0].output == "done-only"

    def test_empty_calls(self, gw: ToolGateway, symbiote_id: str) -> None:
        results = gw.execute_tool_calls(symbiote_id=symbiote_id, session_id=None, calls=[])
        assert results == []

    def test_error_hint_on_policy_failure(self, gw: ToolGateway, symbiote_id: str) -> None:
        """Unregistered tool should produce error with hint."""
        calls = [ToolCall(tool_id="nonexistent", params={})]
        results = gw.execute_tool_calls(symbiote_id=symbiote_id, session_id=None, calls=calls)

        assert len(results) == 1
        assert results[0].success is False
        assert "[Hint:" in (results[0].error or "")


# ── Async: execute_tool_calls_async ──────────────────────────────────────────


class TestAsyncParallel:
    @pytest.mark.asyncio
    async def test_concurrent_execution(
        self, gw: ToolGateway, env_manager: EnvironmentManager, symbiote_id: str,
    ) -> None:
        _configure_tools(gw, env_manager, symbiote_id)

        calls = [
            ToolCall(tool_id="aslow", params={"sleep": 0.15, "tag": str(i)})
            for i in range(4)
        ]
        start = time.monotonic()
        results = await gw.execute_tool_calls_async(
            symbiote_id=symbiote_id, session_id=None, calls=calls,
        )
        elapsed = time.monotonic() - start

        assert elapsed < 0.5, f"Took {elapsed:.2f}s — expected parallel execution"
        assert len(results) == 4
        assert all(r.success for r in results)

    @pytest.mark.asyncio
    async def test_one_failure_doesnt_block_others(
        self, gw: ToolGateway, env_manager: EnvironmentManager, symbiote_id: str,
    ) -> None:
        _configure_tools(gw, env_manager, symbiote_id)

        calls = [
            ToolCall(tool_id="aslow", params={"sleep": 0.05, "tag": "a"}),
            ToolCall(tool_id="afail", params={}),
            ToolCall(tool_id="aslow", params={"sleep": 0.05, "tag": "b"}),
        ]
        results = await gw.execute_tool_calls_async(
            symbiote_id=symbiote_id, session_id=None, calls=calls,
        )

        assert len(results) == 3
        assert results[0].success is True
        assert results[1].success is False
        assert "boom" in (results[1].error or "")
        assert "[Hint:" in (results[1].error or "")
        assert results[2].success is True

    @pytest.mark.asyncio
    async def test_result_order_preserved(
        self, gw: ToolGateway, env_manager: EnvironmentManager, symbiote_id: str,
    ) -> None:
        _configure_tools(gw, env_manager, symbiote_id)

        calls = [
            ToolCall(tool_id="aslow", params={"sleep": 0.1, "tag": "first"}),
            ToolCall(tool_id="aslow", params={"sleep": 0.01, "tag": "second"}),
        ]
        results = await gw.execute_tool_calls_async(
            symbiote_id=symbiote_id, session_id=None, calls=calls,
        )

        assert results[0].output == "done-first"
        assert results[1].output == "done-second"

    @pytest.mark.asyncio
    async def test_single_call(
        self, gw: ToolGateway, env_manager: EnvironmentManager, symbiote_id: str,
    ) -> None:
        _configure_tools(gw, env_manager, symbiote_id)
        calls = [ToolCall(tool_id="aslow", params={"tag": "only"})]
        results = await gw.execute_tool_calls_async(
            symbiote_id=symbiote_id, session_id=None, calls=calls,
        )

        assert len(results) == 1
        assert results[0].success is True
        assert results[0].output == "done-only"

    @pytest.mark.asyncio
    async def test_empty_calls(self, gw: ToolGateway, symbiote_id: str) -> None:
        results = await gw.execute_tool_calls_async(
            symbiote_id=symbiote_id, session_id=None, calls=[],
        )
        assert results == []
