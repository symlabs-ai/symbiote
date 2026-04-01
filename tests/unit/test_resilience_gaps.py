"""Tests covering gaps found in resilience sprint audit.

Covers: async retry bug fix, LoopController circuit breaker integration,
stop_reason in LoopTrace, autocompact within run(), and async parallel
with unregistered tools.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.context import AssembledContext
from symbiote.core.identity import IdentityManager
from symbiote.environment.descriptors import ToolCall, ToolDescriptor
from symbiote.environment.manager import EnvironmentManager
from symbiote.environment.policies import PolicyGate
from symbiote.environment.tools import ToolGateway
from symbiote.runners.chat import ChatRunner

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "gaps_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name="GapBot", role="assistant")
    return sym.id


@pytest.fixture()
def env_manager(adapter: SQLiteAdapter) -> EnvironmentManager:
    return EnvironmentManager(storage=adapter)


@pytest.fixture()
def tool_gateway(env_manager: EnvironmentManager, adapter: SQLiteAdapter) -> ToolGateway:
    gate = PolicyGate(env_manager=env_manager, storage=adapter)
    return ToolGateway(policy_gate=gate)


def _make_context(
    symbiote_id: str,
    user_input: str = "test",
    tools: list[dict] | None = None,
    tool_loop: bool = True,
) -> AssembledContext:
    return AssembledContext(
        symbiote_id=symbiote_id,
        session_id="sess-gap",
        user_input=user_input,
        available_tools=tools or [],
        tool_loop=tool_loop,
    )


# ── B-56 gap: run_async uses retry ──────────────────────────────────────────


class TestAsyncRetryBugFix:
    """Verify that run_async() uses _call_llm_with_retry, not _call_llm_sync."""

    @pytest.mark.asyncio
    @patch("symbiote.runners.chat.time.sleep")
    async def test_run_async_retries_on_transient_error(self, mock_sleep, symbiote_id: str) -> None:
        call_count = 0

        class RetryableLLM:
            def complete(self, messages, config=None, tools=None):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise ConnectionError("transient failure")
                return "Recovered!"

        runner = ChatRunner(RetryableLLM())
        context = _make_context(symbiote_id, tool_loop=False)
        result = await runner.run_async(context)

        assert result.success is True
        assert call_count == 2  # 1 failure + 1 success
        mock_sleep.assert_called_once_with(1)  # backoff delay

    @pytest.mark.asyncio
    @patch("symbiote.runners.chat.time.sleep")
    async def test_run_async_non_retryable_fails_immediately(self, mock_sleep, symbiote_id: str) -> None:
        class BadLLM:
            def complete(self, messages, config=None, tools=None):
                raise ValueError("programming error")

        runner = ChatRunner(BadLLM())
        context = _make_context(symbiote_id, tool_loop=False)
        result = await runner.run_async(context)

        assert result.success is False
        assert "programming error" in result.error
        mock_sleep.assert_not_called()


# ── B-56 gap: run() integration with retry exhaustion ────────────────────────


class TestRunRetryIntegration:
    @patch("symbiote.runners.chat.time.sleep")
    def test_run_returns_failure_after_retry_exhaustion(self, mock_sleep, symbiote_id: str) -> None:
        class AlwaysFailLLM:
            def complete(self, messages, config=None, tools=None):
                raise ConnectionError("always fails")

        runner = ChatRunner(AlwaysFailLLM())
        context = _make_context(symbiote_id, tool_loop=False)
        result = runner.run(context)

        assert result.success is False
        assert "always fails" in result.error


# ── B-57 gap: circuit breaker integration with ChatRunner ────────────────────


class TestCircuitBreakerIntegration:
    def test_circuit_breaker_stops_loop_and_injects(
        self, symbiote_id: str, adapter, env_manager, tool_gateway,
    ) -> None:
        """When a tool fails 3x, LoopController should stop with circuit_breaker."""
        call_count = 0

        class CircuitBreakerLLM:
            def complete(self, messages, config=None, tools=None):
                nonlocal call_count
                call_count += 1
                last_msg = messages[-1]["content"] if messages else ""
                if "unavailable" in last_msg:
                    return "The tool is down. I cannot complete the request."
                block = json.dumps({"tool": "flaky_api", "params": {"attempt": call_count}})
                return f"```tool_call\n{block}\n```"

        # Register a tool that always fails
        tool_gateway.register_tool("flaky_api", lambda p: (_ for _ in ()).throw(RuntimeError("API down")))
        env_manager.configure(symbiote_id=symbiote_id, tools=["flaky_api"])

        runner = ChatRunner(CircuitBreakerLLM(), tool_gateway=tool_gateway)
        context = _make_context(
            symbiote_id,
            "Call the flaky API",
            tools=[{"tool_id": "flaky_api", "name": "Flaky", "description": "Fails", "parameters": {}}],
        )

        result = runner.run(context)
        assert result.success is True
        # Should stop after 3 failures + 1 injection call, not 10 iterations
        assert call_count <= 5


# ── B-57 gap: stop_reason in LoopTrace ───────────────────────────────────────


class TestStopReasonInTrace:
    @pytest.mark.asyncio
    async def test_stagnation_sets_stop_reason(
        self, symbiote_id: str, adapter, env_manager, tool_gateway,
    ) -> None:
        call_count = 0

        class StagnateLLM:
            def complete(self, messages, config=None, tools=None):
                nonlocal call_count
                call_count += 1
                last_msg = messages[-1]["content"] if messages else ""
                if "repeating" in last_msg:
                    return "Done."
                return '```tool_call\n{"tool": "dummy", "params": {"q": "same"}}\n```'

        tool_gateway.register_tool("dummy", lambda p: "ok")
        env_manager.configure(symbiote_id=symbiote_id, tools=["dummy"])

        runner = ChatRunner(StagnateLLM(), tool_gateway=tool_gateway)
        context = _make_context(
            symbiote_id,
            "search",
            tools=[{"tool_id": "dummy", "name": "Dummy", "description": "d", "parameters": {}}],
        )

        result = await runner.run_async(context)
        assert result.success is True
        assert result.loop_trace is not None
        assert result.loop_trace.stop_reason == "stagnation"

    @pytest.mark.asyncio
    async def test_end_turn_sets_stop_reason(self, symbiote_id: str, adapter, env_manager, tool_gateway) -> None:
        call_count = 0

        class OneShotLLM:
            def complete(self, messages, config=None, tools=None):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return '```tool_call\n{"tool": "dummy", "params": {}}\n```'
                return "Done."

        tool_gateway.register_tool("dummy", lambda p: "ok")
        env_manager.configure(symbiote_id=symbiote_id, tools=["dummy"])

        runner = ChatRunner(OneShotLLM(), tool_gateway=tool_gateway)
        context = _make_context(
            symbiote_id,
            "do something",
            tools=[{"tool_id": "dummy", "name": "Dummy", "description": "d", "parameters": {}}],
        )

        result = await runner.run_async(context)
        assert result.success is True
        assert result.loop_trace is not None
        assert result.loop_trace.stop_reason == "end_turn"


# ── B-55 gap: async with unregistered tool ───────────────────────────────────


class TestAsyncUnregisteredTool:
    @pytest.mark.asyncio
    async def test_unregistered_tool_returns_error(self, tool_gateway, symbiote_id: str) -> None:
        calls = [ToolCall(tool_id="nonexistent", params={})]
        results = await tool_gateway.execute_tool_calls_async(
            symbiote_id=symbiote_id, session_id=None, calls=calls,
        )
        assert len(results) == 1
        assert results[0].success is False
        assert "not registered" in (results[0].error or "").lower() or "not allowed" in (results[0].error or "").lower()


# ── B-58 gap: autocompact fires within run() ────────────────────────────────


class TestAutocompactInRun:
    def test_autocompact_fires_during_loop(
        self, symbiote_id: str, adapter, env_manager, tool_gateway,
    ) -> None:
        """With a tiny context_budget, autocompact should fire during the loop."""
        call_count = 0

        class VerboseLLM:
            def complete(self, messages, config=None, tools=None):
                nonlocal call_count
                call_count += 1
                if call_count <= 3:
                    return (
                        f'```tool_call\n'
                        f'{{"tool": "verbose_tool", "params": {{"i": {call_count}}}}}\n'
                        f'```'
                    )
                return "Finished."

        desc = ToolDescriptor(
            tool_id="verbose_tool",
            name="Verbose",
            description="Returns lots of data",
            parameters={"type": "object", "properties": {"i": {"type": "integer"}}},
        )
        tool_gateway.register_descriptor(desc, lambda p: {"data": "x" * 3000})
        env_manager.configure(symbiote_id=symbiote_id, tools=["verbose_tool"])

        # Very small budget — autocompact should trigger
        runner = ChatRunner(VerboseLLM(), tool_gateway=tool_gateway, context_budget=200)
        context = _make_context(
            symbiote_id,
            "process data",
            tools=[desc.model_dump()],
        )

        result = runner.run(context)
        assert result.success is True
        # If autocompact works, the loop should complete without blowing up context
        assert isinstance(result.output, dict)
        assert result.output["text"] == "Finished."
