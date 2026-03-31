"""Tests for LoopController — diminishing returns detection + circuit breaker."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from symbiote.runners.loop_control import LoopController

# ── Unit tests for LoopController ────────────────────────────────────────────


class TestMaxIterations:
    def test_stops_at_max_iterations(self) -> None:
        ctrl = LoopController(max_iterations=3)
        ctrl.record("tool_a", {"x": 1}, True)
        ctrl.record("tool_b", {"y": 2}, True)
        ctrl.record("tool_c", {"z": 3}, True)
        should_stop, reason = ctrl.should_stop()
        assert should_stop is True
        assert reason == "max_iterations"

    def test_does_not_stop_before_max(self) -> None:
        ctrl = LoopController(max_iterations=5)
        ctrl.record("tool_a", {"x": 1}, True)
        ctrl.record("tool_b", {"y": 2}, True)
        should_stop, _ = ctrl.should_stop()
        assert should_stop is False


class TestDuplicateDetection:
    def test_same_tool_same_params_triggers_stagnation(self) -> None:
        ctrl = LoopController(max_iterations=10)
        ctrl.record("search", {"query": "hello"}, True)
        ctrl.record("search", {"query": "hello"}, True)
        should_stop, reason = ctrl.should_stop()
        assert should_stop is True
        assert reason == "stagnation"

    def test_same_tool_different_params_no_stagnation(self) -> None:
        ctrl = LoopController(max_iterations=10)
        ctrl.record("search", {"query": "hello"}, True)
        ctrl.record("search", {"query": "world"}, True)
        should_stop, _ = ctrl.should_stop()
        assert should_stop is False

    def test_different_tools_same_params_no_stagnation(self) -> None:
        ctrl = LoopController(max_iterations=10)
        ctrl.record("tool_a", {"x": 1}, True)
        ctrl.record("tool_b", {"x": 1}, True)
        should_stop, _ = ctrl.should_stop()
        assert should_stop is False

    def test_non_consecutive_duplicates_no_stagnation(self) -> None:
        ctrl = LoopController(max_iterations=10)
        ctrl.record("search", {"query": "hello"}, True)
        ctrl.record("fetch", {"url": "http://example.com"}, True)
        ctrl.record("search", {"query": "hello"}, True)
        should_stop, _ = ctrl.should_stop()
        assert should_stop is False

    def test_params_order_independent(self) -> None:
        """json.dumps(sort_keys=True) makes param order irrelevant."""
        ctrl = LoopController(max_iterations=10)
        ctrl.record("search", {"b": 2, "a": 1}, True)
        ctrl.record("search", {"a": 1, "b": 2}, True)
        should_stop, reason = ctrl.should_stop()
        assert should_stop is True
        assert reason == "stagnation"


class TestCircuitBreaker:
    def test_three_consecutive_failures_triggers_breaker(self) -> None:
        ctrl = LoopController(max_iterations=10)
        ctrl.record("flaky_tool", {"x": 1}, False)
        ctrl.record("flaky_tool", {"x": 2}, False)
        ctrl.record("flaky_tool", {"x": 3}, False)
        should_stop, reason = ctrl.should_stop()
        assert should_stop is True
        assert reason == "circuit_breaker"

    def test_success_resets_failure_count(self) -> None:
        ctrl = LoopController(max_iterations=10)
        ctrl.record("flaky_tool", {"x": 1}, False)
        ctrl.record("flaky_tool", {"x": 2}, False)
        ctrl.record("flaky_tool", {"x": 3}, True)  # success resets
        ctrl.record("flaky_tool", {"x": 4}, False)
        should_stop, _ = ctrl.should_stop()
        assert should_stop is False

    def test_mixed_tools_no_false_trigger(self) -> None:
        """Failures across different tools should not trigger circuit breaker."""
        ctrl = LoopController(max_iterations=10)
        ctrl.record("tool_a", {}, False)
        ctrl.record("tool_b", {}, False)
        ctrl.record("tool_c", {}, False)
        should_stop, _ = ctrl.should_stop()
        assert should_stop is False

    def test_circuit_breaker_per_tool(self) -> None:
        ctrl = LoopController(max_iterations=10)
        ctrl.record("tool_a", {}, False)
        ctrl.record("tool_b", {}, False)
        ctrl.record("tool_a", {}, False)
        # tool_a: 2 failures (non-consecutive count still tracked per record)
        # tool_b: 1 failure
        should_stop, _ = ctrl.should_stop()
        assert should_stop is False


class TestInjectionMessage:
    def test_stagnation_message(self) -> None:
        ctrl = LoopController(max_iterations=10)
        ctrl.record("search", {"q": "x"}, True)
        ctrl.record("search", {"q": "x"}, True)
        msg = ctrl.get_injection_message()
        assert msg is not None
        assert "repeating the same action" in msg

    def test_circuit_breaker_message(self) -> None:
        ctrl = LoopController(max_iterations=10)
        ctrl.record("flaky", {}, False)
        ctrl.record("flaky", {}, False)
        ctrl.record("flaky", {}, False)
        msg = ctrl.get_injection_message()
        assert msg is not None
        assert "flaky" in msg
        assert "unavailable" in msg
        assert "3 times" in msg

    def test_max_iterations_no_injection(self) -> None:
        ctrl = LoopController(max_iterations=2)
        ctrl.record("a", {"x": 1}, True)
        ctrl.record("b", {"y": 2}, True)
        msg = ctrl.get_injection_message()
        assert msg is None

    def test_no_stop_no_injection(self) -> None:
        ctrl = LoopController(max_iterations=10)
        ctrl.record("a", {}, True)
        msg = ctrl.get_injection_message()
        assert msg is None


# ── Integration test with ChatRunner ─────────────────────────────────────────


class _ToolCallLLM:
    """Mock LLM that always calls the same tool, then responds cleanly on injection."""

    def __init__(self, tool_id: str, params: dict, final_response: str = "Done.") -> None:
        self._tool_id = tool_id
        self._params = params
        self._final_response = final_response
        self._call_count = 0

    def complete(
        self,
        messages: list[dict],
        config: dict | None = None,
        tools: list[dict] | None = None,
    ) -> str:
        self._call_count += 1
        # When injection message appears, respond cleanly
        last_msg = messages[-1]["content"] if messages else ""
        if "repeating the same action" in last_msg or "unavailable" in last_msg:
            return self._final_response
        # Otherwise keep calling the same tool
        block = json.dumps({"tool": self._tool_id, "params": self._params})
        return f"```tool_call\n{block}\n```"


class TestChatRunnerIntegration:
    """Test that ChatRunner stops when LoopController detects problems."""

    def test_stagnation_stops_loop(self, tmp_path: Path) -> None:
        from symbiote.adapters.storage.sqlite import SQLiteAdapter
        from symbiote.core.context import AssembledContext
        from symbiote.core.identity import IdentityManager
        from symbiote.environment.manager import EnvironmentManager
        from symbiote.environment.policies import PolicyGate
        from symbiote.environment.tools import ToolGateway
        from symbiote.runners.chat import ChatRunner

        db = tmp_path / "test.db"
        adapter = SQLiteAdapter(db_path=db)
        adapter.init_schema()

        mgr = IdentityManager(storage=adapter)
        sym = mgr.create(name="TestBot", role="assistant")

        env_mgr = EnvironmentManager(storage=adapter)
        gate = PolicyGate(env_manager=env_mgr, storage=adapter)
        gw = ToolGateway(policy_gate=gate)

        # Register a dummy tool via the gateway
        gw.register_tool("dummy_search", lambda p: {"result": "found"})
        env_mgr.configure(symbiote_id=sym.id, tools=["dummy_search"])

        llm = _ToolCallLLM("dummy_search", {"q": "test"}, "I could not find more results.")
        runner = ChatRunner(llm, tool_gateway=gw)

        context = AssembledContext(
            symbiote_id=sym.id,
            session_id="sess-1",
            user_input="Search for something",
            available_tools=[
                {"tool_id": "dummy_search", "name": "Dummy Search",
                 "description": "A dummy search tool", "parameters": {}},
            ],
            tool_loop=True,
        )

        result = runner.run(context)
        assert result.success is True
        # The LLM should have been called a small number of times (not 10)
        # 2 tool calls (stagnation detected on 2nd) + 1 injection call = 3
        assert llm._call_count <= 4

        adapter.close()
