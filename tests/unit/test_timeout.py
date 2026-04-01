"""Tests for per-tool and loop timeout — B-33."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.context import AssembledContext
from symbiote.core.identity import IdentityManager
from symbiote.core.models import EnvironmentConfig
from symbiote.environment.descriptors import (
    LLMResponse,
    ToolCall,
    ToolCallResult,
    ToolDescriptor,
)
from symbiote.environment.manager import EnvironmentManager
from symbiote.environment.policies import PolicyGate, ToolResult
from symbiote.environment.tools import ToolGateway
from symbiote.runners.chat import ChatRunner

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "timeout_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name="TimeoutBot", role="assistant")
    return sym.id


@pytest.fixture()
def env_manager(adapter: SQLiteAdapter) -> EnvironmentManager:
    return EnvironmentManager(storage=adapter)


@pytest.fixture()
def gate(env_manager: EnvironmentManager, adapter: SQLiteAdapter) -> PolicyGate:
    return PolicyGate(env_manager=env_manager, storage=adapter)


@pytest.fixture()
def gateway(gate: PolicyGate) -> ToolGateway:
    return ToolGateway(policy_gate=gate)


# ── EnvironmentConfig model tests ────────────────────────────────────────────


class TestEnvironmentConfigModel:
    def test_default_values(self) -> None:
        cfg = EnvironmentConfig(symbiote_id="s1")
        assert cfg.tool_call_timeout == 30.0
        assert cfg.loop_timeout == 300.0

    def test_custom_values(self) -> None:
        cfg = EnvironmentConfig(
            symbiote_id="s1",
            tool_call_timeout=10.0,
            loop_timeout=60.0,
        )
        assert cfg.tool_call_timeout == 10.0
        assert cfg.loop_timeout == 60.0

    def test_validation_bounds(self) -> None:
        # tool_call_timeout bounds: ge=1.0, le=300.0
        with pytest.raises(ValidationError):
            EnvironmentConfig(symbiote_id="s1", tool_call_timeout=0.5)
        with pytest.raises(ValidationError):
            EnvironmentConfig(symbiote_id="s1", tool_call_timeout=301.0)
        # loop_timeout bounds: ge=10.0, le=3600.0
        with pytest.raises(ValidationError):
            EnvironmentConfig(symbiote_id="s1", loop_timeout=5.0)
        with pytest.raises(ValidationError):
            EnvironmentConfig(symbiote_id="s1", loop_timeout=3601.0)


# ── EnvironmentManager round-trip ─────────────────────────────────────────────


class TestEnvironmentManagerRoundTrip:
    def test_configure_and_read_timeout_defaults(
        self,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        env_manager.configure(symbiote_id=symbiote_id, tools=["fs_read"])
        assert env_manager.get_tool_call_timeout(symbiote_id) == 30.0
        assert env_manager.get_loop_timeout(symbiote_id) == 300.0

    def test_configure_and_read_custom_timeouts(
        self,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        env_manager.configure(
            symbiote_id=symbiote_id,
            tools=["fs_read"],
            tool_call_timeout=15.0,
            loop_timeout=120.0,
        )
        assert env_manager.get_tool_call_timeout(symbiote_id) == 15.0
        assert env_manager.get_loop_timeout(symbiote_id) == 120.0

    def test_update_preserves_timeouts(
        self,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        env_manager.configure(
            symbiote_id=symbiote_id,
            tools=["fs_read"],
            tool_call_timeout=15.0,
            loop_timeout=120.0,
        )
        # Update only tools, timeouts should be preserved
        env_manager.configure(symbiote_id=symbiote_id, tools=["fs_read", "fs_write"])
        assert env_manager.get_tool_call_timeout(symbiote_id) == 15.0
        assert env_manager.get_loop_timeout(symbiote_id) == 120.0

    def test_no_config_returns_defaults(
        self,
        env_manager: EnvironmentManager,
    ) -> None:
        assert env_manager.get_tool_call_timeout("nonexistent") == 30.0
        assert env_manager.get_loop_timeout("nonexistent") == 300.0


# ── Per-tool timeout (sync) ──────────────────────────────────────────────────


class TestToolTimeoutSync:
    def test_slow_tool_times_out(
        self,
        gateway: ToolGateway,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        env_manager.configure(symbiote_id=symbiote_id, tools=["slow_tool"])

        def slow_handler(params: dict) -> str:
            time.sleep(2)
            return "done"

        desc = ToolDescriptor(
            tool_id="slow_tool",
            name="Slow Tool",
            description="A slow tool",
            parameters={"type": "object", "properties": {}},
            handler_type="builtin",
        )
        gateway.register_descriptor(desc, slow_handler)

        result = gateway.execute(
            symbiote_id=symbiote_id,
            session_id=None,
            tool_id="slow_tool",
            params={},
            timeout=0.5,
        )
        assert result.success is False
        assert "timed out" in result.error

    def test_fast_tool_succeeds(
        self,
        gateway: ToolGateway,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        env_manager.configure(symbiote_id=symbiote_id, tools=["fast_tool"])

        def fast_handler(params: dict) -> str:
            return "fast result"

        desc = ToolDescriptor(
            tool_id="fast_tool",
            name="Fast Tool",
            description="A fast tool",
            parameters={"type": "object", "properties": {}},
            handler_type="builtin",
        )
        gateway.register_descriptor(desc, fast_handler)

        result = gateway.execute(
            symbiote_id=symbiote_id,
            session_id=None,
            tool_id="fast_tool",
            params={},
            timeout=1.0,
        )
        assert result.success is True
        assert result.output == "fast result"


# ── Per-tool timeout (async) ─────────────────────────────────────────────────


class TestToolTimeoutAsync:
    @pytest.mark.asyncio
    async def test_slow_tool_times_out_async(
        self,
        gateway: ToolGateway,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        env_manager.configure(symbiote_id=symbiote_id, tools=["slow_async"])

        def slow_handler(params: dict) -> str:
            time.sleep(2)
            return "done"

        desc = ToolDescriptor(
            tool_id="slow_async",
            name="Slow Async",
            description="A slow tool",
            parameters={"type": "object", "properties": {}},
            handler_type="builtin",
        )
        gateway.register_descriptor(desc, slow_handler)

        result = await gateway.execute_async(
            symbiote_id=symbiote_id,
            session_id=None,
            tool_id="slow_async",
            params={},
            timeout=0.5,
        )
        assert result.success is False
        assert "timed out" in result.error

    @pytest.mark.asyncio
    async def test_fast_tool_succeeds_async(
        self,
        gateway: ToolGateway,
        env_manager: EnvironmentManager,
        symbiote_id: str,
    ) -> None:
        env_manager.configure(symbiote_id=symbiote_id, tools=["fast_async"])

        def fast_handler(params: dict) -> str:
            return "async fast result"

        desc = ToolDescriptor(
            tool_id="fast_async",
            name="Fast Async",
            description="A fast tool",
            parameters={"type": "object", "properties": {}},
            handler_type="builtin",
        )
        gateway.register_descriptor(desc, fast_handler)

        result = await gateway.execute_async(
            symbiote_id=symbiote_id,
            session_id=None,
            tool_id="fast_async",
            params={},
            timeout=1.0,
        )
        assert result.success is True
        assert result.output == "async fast result"


# ── Loop timeout ──────────────────────────────────────────────────────────────


class TestLoopTimeout:
    def test_loop_stops_after_timeout(self) -> None:
        """Mock LLM that always returns tool calls; loop must stop after timeout."""
        call_count = 0

        class ToolCallLLM:
            def complete(self, messages, config=None, tools=None):
                nonlocal call_count
                call_count += 1
                # Always return a tool call so the loop continues
                return '```tool_call\n{"tool": "slow_tool", "params": {}}\n```'

        mock_gateway = MagicMock(spec=ToolGateway)

        def fake_execute_tool_calls(symbiote_id, session_id, calls, timeout=30.0):
            # Simulate tool execution that takes some time
            time.sleep(0.3)
            return [
                ToolCallResult(
                    tool_id="slow_tool",
                    success=True,
                    output="ok",
                    error=None,
                )
            ]

        mock_gateway.execute_tool_calls = fake_execute_tool_calls

        runner = ChatRunner(
            llm=ToolCallLLM(),
            tool_gateway=mock_gateway,
        )

        context = AssembledContext(
            symbiote_id="s1",
            session_id="sess1",
            user_input="do something",
            tool_loop=True,
            max_tool_iterations=50,
            loop_timeout=1.0,  # 1 second timeout
            tool_call_timeout=5.0,
            available_tools=[{
                "tool_id": "slow_tool",
                "name": "Slow Tool",
                "description": "A tool",
                "parameters": {"type": "object", "properties": {}},
            }],
        )

        result = runner.run(context)
        # Loop should have stopped well before 50 iterations due to timeout
        assert call_count < 50
        assert result.success is True


# ── AssembledContext defaults ─────────────────────────────────────────────────


class TestAssembledContextDefaults:
    def test_defaults_preserved(self) -> None:
        ctx = AssembledContext(
            symbiote_id="s1",
            session_id="sess1",
            user_input="hello",
        )
        assert ctx.tool_call_timeout == 30.0
        assert ctx.loop_timeout == 300.0

    def test_custom_values(self) -> None:
        ctx = AssembledContext(
            symbiote_id="s1",
            session_id="sess1",
            user_input="hello",
            tool_call_timeout=10.0,
            loop_timeout=60.0,
        )
        assert ctx.tool_call_timeout == 10.0
        assert ctx.loop_timeout == 60.0
