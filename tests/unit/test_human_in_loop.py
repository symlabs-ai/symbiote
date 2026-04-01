"""Tests for human-in-the-loop: risk_level on ToolDescriptor + approval callback on ChatRunner."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.context import AssembledContext
from symbiote.core.identity import IdentityManager
from symbiote.environment.descriptors import (
    ToolCallResult,
    ToolDescriptor,
)
from symbiote.environment.manager import EnvironmentManager
from symbiote.environment.policies import PolicyGate
from symbiote.environment.tools import ToolGateway
from symbiote.runners.chat import ChatRunner

# ── Helpers ──────────────────────────────────────────────────────────────────


class MockLLM:
    """Mock LLM that returns a sequence of responses (tool call then final)."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self._idx = 0

    def complete(
        self,
        messages: list[dict],
        config: dict | None = None,
        tools: list[dict] | None = None,
    ) -> str:
        resp = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1
        return resp


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "hitl_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name="TestBot", role="assistant")
    return sym.id


@pytest.fixture()
def tool_gateway(adapter: SQLiteAdapter) -> ToolGateway:
    env_mgr = EnvironmentManager(storage=adapter)
    gate = PolicyGate(env_manager=env_mgr, storage=adapter)
    return ToolGateway(policy_gate=gate)


def _make_context(
    symbiote_id: str,
    user_input: str,
    tools: list[dict] | None = None,
    tool_loop: bool = False,
) -> AssembledContext:
    return AssembledContext(
        symbiote_id=symbiote_id,
        session_id="sess-hitl",
        user_input=user_input,
        available_tools=tools or [],
        tool_loop=tool_loop,
    )


# ── ToolDescriptor risk_level ────────────────────────────────────────────────


class TestToolDescriptorRiskLevel:
    def test_default_risk_level_is_low(self) -> None:
        desc = ToolDescriptor(
            tool_id="my_tool", name="My Tool", description="A tool"
        )
        assert desc.risk_level == "low"

    def test_risk_level_medium(self) -> None:
        desc = ToolDescriptor(
            tool_id="my_tool",
            name="My Tool",
            description="A tool",
            risk_level="medium",
        )
        assert desc.risk_level == "medium"

    def test_risk_level_high(self) -> None:
        desc = ToolDescriptor(
            tool_id="danger_tool",
            name="Danger",
            description="Destructive",
            risk_level="high",
        )
        assert desc.risk_level == "high"

    def test_risk_level_in_serialization(self) -> None:
        desc = ToolDescriptor(
            tool_id="t", name="T", description="D", risk_level="high"
        )
        data = desc.model_dump()
        assert data["risk_level"] == "high"

    def test_risk_level_propagation_through_gateway(
        self, tool_gateway: ToolGateway
    ) -> None:
        desc = ToolDescriptor(
            tool_id="publish_article",
            name="Publish",
            description="Publish an article",
            risk_level="high",
        )
        tool_gateway.register_descriptor(desc, lambda params: "ok")
        assert tool_gateway.get_risk_level("publish_article") == "high"

    def test_gateway_default_risk_for_unknown_tool(
        self, tool_gateway: ToolGateway
    ) -> None:
        assert tool_gateway.get_risk_level("nonexistent") == "low"


# ── ToolCallResult risk_level ────────────────────────────────────────────────


class TestToolCallResultRiskLevel:
    def test_default_risk_level_is_none(self) -> None:
        r = ToolCallResult(tool_id="t", success=True)
        assert r.risk_level is None

    def test_risk_level_set(self) -> None:
        r = ToolCallResult(tool_id="t", success=False, risk_level="high")
        assert r.risk_level == "high"


# ── Approval callback on ChatRunner ──────────────────────────────────────────


class TestApprovalCallback:
    def test_callback_approves_high_risk_tool_executes(
        self, symbiote_id: str, tool_gateway: ToolGateway
    ) -> None:
        """When callback returns True, the high-risk tool executes normally."""
        desc = ToolDescriptor(
            tool_id="send_email",
            name="Send Email",
            description="Send an email",
            risk_level="high",
            parameters={
                "type": "object",
                "properties": {"to": {"type": "string"}},
            },
        )
        tool_gateway.register_descriptor(desc, lambda params: "email sent")

        # LLM: first call produces tool_call, second call is final response
        llm = MockLLM([
            '```tool_call\n{"tool": "send_email", "params": {"to": "user@example.com"}}\n```',
            "Email has been sent successfully.",
        ])

        approvals: list[tuple] = []

        def approve_all(tool_id: str, params: dict, risk: str) -> bool:
            approvals.append((tool_id, params, risk))
            return True

        runner = ChatRunner(
            llm,
            tool_gateway=tool_gateway,
            on_before_tool_call=approve_all,
        )

        tools = [desc.model_dump()]
        ctx = _make_context(symbiote_id, "Send email to user", tools=tools, tool_loop=True)
        result = runner.run(ctx)

        assert result.success
        assert len(approvals) == 1
        assert approvals[0][0] == "send_email"
        assert approvals[0][2] == "high"

    def test_callback_denies_high_risk_tool_skipped(
        self, symbiote_id: str, tool_gateway: ToolGateway
    ) -> None:
        """When callback returns False, the tool is skipped with an error."""
        desc = ToolDescriptor(
            tool_id="delete_all",
            name="Delete All",
            description="Delete everything",
            risk_level="high",
            parameters={"type": "object", "properties": {}},
        )
        tool_gateway.register_descriptor(desc, lambda params: "deleted")

        llm = MockLLM([
            '```tool_call\n{"tool": "delete_all", "params": {}}\n```',
            "The deletion was denied.",
        ])

        def deny_all(tool_id: str, params: dict, risk: str) -> bool:
            return False

        runner = ChatRunner(
            llm,
            tool_gateway=tool_gateway,
            on_before_tool_call=deny_all,
        )

        tools = [desc.model_dump()]
        ctx = _make_context(symbiote_id, "Delete everything", tools=tools, tool_loop=True)
        result = runner.run(ctx)

        assert result.success
        # The output should contain tool_results with the denial
        output = result.output
        if isinstance(output, dict):
            tool_results = output.get("tool_results", [])
            denied = [r for r in tool_results if r.get("error") and "denied" in r["error"]]
            assert len(denied) >= 1
            assert denied[0]["tool_id"] == "delete_all"

    def test_no_callback_all_tools_execute(
        self, symbiote_id: str, tool_gateway: ToolGateway
    ) -> None:
        """When no callback is set, all tools execute (backward compat)."""
        desc = ToolDescriptor(
            tool_id="publish",
            name="Publish",
            description="Publish content",
            risk_level="high",
            parameters={"type": "object", "properties": {}},
        )
        tool_gateway.register_descriptor(desc, lambda params: "published")

        llm = MockLLM([
            '```tool_call\n{"tool": "publish", "params": {}}\n```',
            "Content published.",
        ])

        # No on_before_tool_call — backward compat
        runner = ChatRunner(llm, tool_gateway=tool_gateway)

        tools = [desc.model_dump()]
        ctx = _make_context(symbiote_id, "Publish it", tools=tools, tool_loop=True)
        result = runner.run(ctx)

        assert result.success
        # Tool should have executed (no denial)
        output = result.output
        if isinstance(output, dict):
            tool_results = output.get("tool_results", [])
            for r in tool_results:
                assert "denied" not in (r.get("error") or "")

    def test_low_risk_tool_skips_callback(
        self, symbiote_id: str, tool_gateway: ToolGateway
    ) -> None:
        """Low-risk tools are never passed to the callback."""
        desc = ToolDescriptor(
            tool_id="list_items",
            name="List Items",
            description="List all items",
            risk_level="low",
            parameters={"type": "object", "properties": {}},
        )
        tool_gateway.register_descriptor(desc, lambda params: ["item1", "item2"])

        llm = MockLLM([
            '```tool_call\n{"tool": "list_items", "params": {}}\n```',
            "Here are the items.",
        ])

        callback_calls: list[str] = []

        def track_callback(tool_id: str, params: dict, risk: str) -> bool:
            callback_calls.append(tool_id)
            return False  # deny — but should never be called for low risk

        runner = ChatRunner(
            llm,
            tool_gateway=tool_gateway,
            on_before_tool_call=track_callback,
        )

        tools = [desc.model_dump()]
        ctx = _make_context(symbiote_id, "List items", tools=tools, tool_loop=True)
        result = runner.run(ctx)

        assert result.success
        # Callback should never have been called for low-risk tool
        assert len(callback_calls) == 0

    def test_medium_risk_tool_skips_callback(
        self, symbiote_id: str, tool_gateway: ToolGateway
    ) -> None:
        """Medium-risk tools are auto-approved (not passed to callback)."""
        desc = ToolDescriptor(
            tool_id="update_record",
            name="Update Record",
            description="Update a record",
            risk_level="medium",
            parameters={"type": "object", "properties": {}},
        )
        tool_gateway.register_descriptor(desc, lambda params: "updated")

        llm = MockLLM([
            '```tool_call\n{"tool": "update_record", "params": {}}\n```',
            "Record updated.",
        ])

        callback_calls: list[str] = []

        def track_callback(tool_id: str, params: dict, risk: str) -> bool:
            callback_calls.append(tool_id)
            return False

        runner = ChatRunner(
            llm,
            tool_gateway=tool_gateway,
            on_before_tool_call=track_callback,
        )

        tools = [desc.model_dump()]
        ctx = _make_context(symbiote_id, "Update it", tools=tools, tool_loop=True)
        result = runner.run(ctx)

        assert result.success
        assert len(callback_calls) == 0

    def test_denial_error_fed_back_to_llm(
        self, symbiote_id: str, tool_gateway: ToolGateway
    ) -> None:
        """When a tool is denied, the LLM receives the denial as a tool error."""
        desc = ToolDescriptor(
            tool_id="nuke",
            name="Nuke",
            description="Nuclear option",
            risk_level="high",
            parameters={"type": "object", "properties": {}},
        )
        tool_gateway.register_descriptor(desc, lambda params: "boom")

        received_messages: list[list[dict]] = []

        class CaptureLLM:
            def __init__(self) -> None:
                self._call_count = 0

            def complete(
                self,
                messages: list[dict],
                config: dict | None = None,
                tools: list[dict] | None = None,
            ) -> str:
                received_messages.append(list(messages))
                self._call_count += 1
                if self._call_count == 1:
                    return '```tool_call\n{"tool": "nuke", "params": {}}\n```'
                return "The action was blocked."

        runner = ChatRunner(
            CaptureLLM(),
            tool_gateway=tool_gateway,
            on_before_tool_call=lambda tid, p, r: False,
        )

        tools = [desc.model_dump()]
        ctx = _make_context(symbiote_id, "Do it", tools=tools, tool_loop=True)
        runner.run(ctx)

        # Second LLM call should contain the denial error in messages
        assert len(received_messages) >= 2
        last_messages = received_messages[-1]
        # Find the tool result message that contains the denial
        denial_msgs = [
            m for m in last_messages
            if m["role"] == "user" and "denied" in m.get("content", "").lower()
        ]
        assert len(denial_msgs) >= 1
