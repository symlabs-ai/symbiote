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
        # ``A, B, A`` (3 calls, 2 unique keys) is legitimate "let me
        # double-check" behavior. Ping-pong detection only fires when
        # the same low-diversity pattern persists for ``_PING_PONG_WINDOW``
        # (=5) calls. See test_ping_pong_fires_at_window_size below.
        ctrl = LoopController(max_iterations=10)
        ctrl.record("search", {"query": "hello"}, True)
        ctrl.record("fetch", {"url": "http://example.com"}, True)
        ctrl.record("search", {"query": "hello"}, True)
        should_stop, _ = ctrl.should_stop()
        assert should_stop is False

    def test_ping_pong_fires_at_window_size(self) -> None:
        """A,B,A,B,A — same 2 keys cycled across the trailing 5-call window.

        Real case from sym_talk_lt 2026-05-27 ("qual a versão do Python"
        smoke): LLM alternated web_search "Python latest" ↔ web_extract
        python.org/downloads, hitting the dedup cache every time but
        never repeating consecutively (which would have triggered the
        existing A,A check). The ping-pong heuristic catches exactly
        this: 5 calls, ≤ 2 unique signatures.
        """
        ctrl = LoopController(max_iterations=20)
        ctrl.record("search", {"q": "x"}, True)
        ctrl.record("extract", {"url": "u"}, True)
        ctrl.record("search", {"q": "x"}, True)
        ctrl.record("extract", {"url": "u"}, True)
        ctrl.record("search", {"q": "x"}, True)
        should_stop, reason = ctrl.should_stop()
        assert should_stop is True
        assert reason == "stagnation"

    def test_ping_pong_three_unique_no_stop(self) -> None:
        """A,B,C,A,B — 3 unique keys → legitimate multi-source exploration."""
        ctrl = LoopController(max_iterations=20)
        ctrl.record("search", {"q": "x"}, True)
        ctrl.record("extract", {"url": "u1"}, True)
        ctrl.record("extract", {"url": "u2"}, True)
        ctrl.record("search", {"q": "x"}, True)
        ctrl.record("extract", {"url": "u1"}, True)
        should_stop, _ = ctrl.should_stop()
        assert should_stop is False

    def test_ping_pong_below_window_no_stop(self) -> None:
        """A,B,A,B — only 4 calls, still under the 5-call window."""
        ctrl = LoopController(max_iterations=20)
        ctrl.record("search", {"q": "x"}, True)
        ctrl.record("extract", {"url": "u"}, True)
        ctrl.record("search", {"q": "x"}, True)
        ctrl.record("extract", {"url": "u"}, True)
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

    def test_injection_call_omits_tools_kwarg(self, tmp_path: Path) -> None:
        """The post-stagnation injection call must pass ``tools=None``.

        Without dropping tools the LLM is free to emit yet another
        tool_call instead of plain text, producing the "Empty response
        after N tool calls" deterministic fallback observed in sym_talk_lt
        2026-05-27 11:49 ("O Corinthians joga hoje?"). The fix is in
        chat.py: ``inj_kwargs = dict(kwargs); inj_kwargs.pop("tools", None)``
        right before the injection ``_call_llm_sync``. This test pins
        that behavior so the fix can't regress silently.
        """
        from symbiote.adapters.storage.sqlite import SQLiteAdapter
        from symbiote.core.context import AssembledContext
        from symbiote.core.identity import IdentityManager
        from symbiote.environment.manager import EnvironmentManager
        from symbiote.environment.policies import PolicyGate
        from symbiote.environment.tools import ToolGateway
        from symbiote.runners.chat import ChatRunner

        class _CaptureLLM:
            """Records each call's `tools` kwarg + always emits a native tool_call.

            Uses the same OpenAI-shaped tool_calls structure ChatRunner
            consumes when ``native_tools=True``; without that flag the
            runner builds the catalog inside the prompt text instead of
            passing ``tools=[...]`` to the adapter, defeating the test.
            """
            def __init__(self) -> None:
                self.calls: list[dict] = []
                self._n = 0

            def complete(self, messages, config=None, tools=None):
                self._n += 1
                last_msg = messages[-1]["content"] if messages else ""
                self.calls.append({
                    "tools_present": tools is not None,
                    "tools_count": len(tools) if tools else 0,
                    "last_msg": last_msg,
                })
                if "repeating the same action" in last_msg:
                    return "Aqui está a resposta sintetizada."
                # Emit a native LLMResponse with a NativeToolCall so the
                # ChatRunner's native_tools=True path executes the tool
                # and continues the loop.
                from symbiote.environment.descriptors import LLMResponse, NativeToolCall
                return LLMResponse(
                    content="",
                    tool_calls=[NativeToolCall(
                        call_id=f"call_{self._n}",
                        tool_id="dummy_search",
                        params={"q": "test"},
                    )],
                )

        db = tmp_path / "capture.db"
        adapter = SQLiteAdapter(db_path=db)
        adapter.init_schema()
        mgr = IdentityManager(storage=adapter)
        sym = mgr.create(name="TestBot2", role="assistant")
        env_mgr = EnvironmentManager(storage=adapter)
        gate = PolicyGate(env_manager=env_mgr, storage=adapter)
        gw = ToolGateway(policy_gate=gate)
        gw.register_tool("dummy_search", lambda p: {"result": "found"})
        env_mgr.configure(symbiote_id=sym.id, tools=["dummy_search"])

        llm = _CaptureLLM()
        runner = ChatRunner(llm, tool_gateway=gw, native_tools=True)
        context = AssembledContext(
            symbiote_id=sym.id, session_id="sess-cap", user_input="Search",
            available_tools=[
                {"tool_id": "dummy_search", "name": "Dummy Search",
                 "description": "dummy", "parameters": {}},
            ],
            tool_loop=True,
        )
        runner.run(context)
        adapter.close()

        # The runner makes ≥3 calls: at least 2 tool-emitting + 1 injection.
        assert len(llm.calls) >= 3
        # Tool-emitting calls all see ``tools`` populated (the catalog).
        for call in llm.calls[:-1]:
            assert call["tools_present"] is True, "tool calls should expose tools"

        # The FINAL call must be the injection — recognizable by the
        # injection message landing as the last user message — and it
        # must have NO tools exposed.
        last = llm.calls[-1]
        assert "repeating the same action" in last["last_msg"], (
            "expected the last LLM call to be the post-stagnation injection"
        )
        assert last["tools_present"] is False, (
            "injection call must drop tools to force text output"
        )
