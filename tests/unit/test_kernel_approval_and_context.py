"""Tests for kernel.set_approval_callback and contextvar propagation into handlers.

Covers two embedded-host guarantees:

1. ``kernel.set_approval_callback`` actually gates tool execution that flows
   through ``kernel.message()`` (the internal ChatRunner), not just a
   standalone ChatRunner.
2. Host contextvars (e.g. current-user / auth state) set on the calling
   thread survive into synchronous tool handlers dispatched on the gateway's
   thread pool — single-call and parallel paths.
"""

from __future__ import annotations

import contextvars
from pathlib import Path

import pytest

from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel
from symbiote.environment.descriptors import ToolDescriptor
from symbiote.environment.manager import EnvironmentManager
from symbiote.environment.policies import PolicyGate
from symbiote.environment.tools import ToolGateway

# ── helpers ──────────────────────────────────────────────────────────────────


class ScriptedLLM:
    """LLM that emits a single tool_call on the first turn, then plain text."""

    def __init__(self, tool_id: str) -> None:
        self._tool_id = tool_id
        self._calls = 0

    def complete(self, messages, config=None, tools=None) -> str:  # noqa: ANN001
        self._calls += 1
        if self._calls == 1:
            return (
                "```tool_call\n"
                f'{{"tool": "{self._tool_id}", "params": {{}}}}\n'
                "```"
            )
        return "done"


# ── set_approval_callback ─────────────────────────────────────────────────────


class TestSetApprovalCallback:
    def test_high_risk_tool_denied_via_message(self, tmp_path: Path) -> None:
        kernel = SymbioteKernel(
            config=KernelConfig(db_path=tmp_path / "k.db"),
            llm=ScriptedLLM("delete_goal"),
        )
        sym = kernel.create_symbiote(name="ChopChop", role="assistant")

        executed: list[str] = []

        def handler(params: dict) -> str:
            executed.append("ran")
            return "deleted"

        kernel.tool_gateway.register_descriptor(
            ToolDescriptor(
                tool_id="delete_goal",
                name="Delete Goal",
                description="Delete a goal",
                handler_type="custom",
                risk_level="high",
            ),
            handler,
        )
        kernel.environment.configure(symbiote_id=sym.id, tools=["delete_goal"])

        seen: list[tuple[str, str]] = []

        def gate(tool_id: str, params: dict, risk: str) -> bool:
            seen.append((tool_id, risk))
            return False  # deny

        kernel.set_approval_callback(gate)

        session = kernel.start_session(sym.id)
        kernel.message(session_id=session.id, content="delete my goal")

        assert seen == [("delete_goal", "high")]  # gate was consulted
        assert executed == []  # handler never ran — denied
        kernel.shutdown()

    def test_high_risk_tool_allowed_when_gate_returns_true(self, tmp_path: Path) -> None:
        kernel = SymbioteKernel(
            config=KernelConfig(db_path=tmp_path / "k2.db"),
            llm=ScriptedLLM("delete_goal"),
        )
        sym = kernel.create_symbiote(name="ChopChop", role="assistant")

        executed: list[str] = []
        kernel.tool_gateway.register_descriptor(
            ToolDescriptor(
                tool_id="delete_goal", name="Delete Goal",
                description="Delete a goal", handler_type="custom",
                risk_level="high",
            ),
            lambda params: executed.append("ran") or "deleted",
        )
        kernel.environment.configure(symbiote_id=sym.id, tools=["delete_goal"])
        kernel.set_approval_callback(lambda t, p, r: True)

        session = kernel.start_session(sym.id)
        kernel.message(session_id=session.id, content="delete my goal")

        assert executed == ["ran"]
        kernel.shutdown()

    def test_raises_without_llm(self, tmp_path: Path) -> None:
        kernel = SymbioteKernel(config=KernelConfig(db_path=tmp_path / "k3.db"))
        with pytest.raises(RuntimeError):
            kernel.set_approval_callback(lambda t, p, r: True)
        kernel.shutdown()


# ── contextvar propagation into handlers ──────────────────────────────────────

_current_user: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_user", default="anonymous"
)


@pytest.fixture()
def gateway(tmp_path: Path):
    from symbiote.adapters.storage.sqlite import SQLiteAdapter
    from symbiote.core.identity import IdentityManager

    adp = SQLiteAdapter(db_path=tmp_path / "ctx.db")
    adp.init_schema()
    sym = IdentityManager(storage=adp).create(name="X", role="assistant")
    env = EnvironmentManager(adp)
    gate = PolicyGate(env, adp)
    gw = ToolGateway(gate)
    env.configure(symbiote_id=sym.id, tools=[])
    yield gw, sym.id, env, adp
    adp.close()


def _register_user_echo(gw: ToolGateway, env, sym_id: str, tool_id: str) -> None:
    def handler(params: dict) -> str:
        return _current_user.get()  # reads from contextvar, not params

    gw.register_descriptor(
        ToolDescriptor(tool_id=tool_id, name=tool_id, description="echo user",
                       handler_type="custom"),
        handler,
    )
    env.configure(symbiote_id=sym_id, tools=gw.list_tools())


class TestContextvarPropagation:
    def test_single_call_sees_caller_contextvar(self, gateway) -> None:
        gw, sym_id, env, _ = gateway
        _register_user_echo(gw, env, sym_id, "whoami")

        from symbiote.environment.descriptors import ToolCall

        token = _current_user.set("user-42")
        try:
            results = gw.execute_tool_calls(
                symbiote_id=sym_id, session_id="s1",
                calls=[ToolCall(tool_id="whoami", params={})],
            )
        finally:
            _current_user.reset(token)

        assert results[0].success
        assert results[0].output == "user-42"

    def test_parallel_calls_see_caller_contextvar(self, gateway) -> None:
        gw, sym_id, env, _ = gateway
        _register_user_echo(gw, env, sym_id, "whoami_a")

        def handler_b(params: dict) -> str:
            return _current_user.get()

        gw.register_descriptor(
            ToolDescriptor(tool_id="whoami_b", name="whoami_b",
                           description="echo", handler_type="custom"),
            handler_b,
        )
        env.configure(symbiote_id=sym_id, tools=gw.list_tools())

        from symbiote.environment.descriptors import ToolCall

        token = _current_user.set("user-99")
        try:
            results = gw.execute_tool_calls(
                symbiote_id=sym_id, session_id="s1",
                calls=[
                    ToolCall(tool_id="whoami_a", params={}),
                    ToolCall(tool_id="whoami_b", params={}),
                ],
            )
        finally:
            _current_user.reset(token)

        assert all(r.success for r in results)
        assert {r.output for r in results} == {"user-99"}
