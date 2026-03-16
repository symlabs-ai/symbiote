"""Tests for ChatRunner with tool execution."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.context import AssembledContext
from symbiote.core.identity import IdentityManager
from symbiote.core.ports import LLMPort
from symbiote.environment.descriptors import ToolDescriptor
from symbiote.environment.manager import EnvironmentManager
from symbiote.environment.policies import PolicyGate
from symbiote.environment.tools import ToolGateway
from symbiote.runners.chat import ChatRunner


class MockLLM:
    """Mock LLM that returns a predetermined response."""

    def __init__(self, response: str) -> None:
        self._response = response

    def complete(self, messages: list[dict], config: dict | None = None) -> str:
        return self._response


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "chat_tools_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name="ChatBot", role="assistant")
    return sym.id


@pytest.fixture()
def env_manager(adapter: SQLiteAdapter) -> EnvironmentManager:
    return EnvironmentManager(storage=adapter)


@pytest.fixture()
def tool_gateway(env_manager: EnvironmentManager, adapter: SQLiteAdapter) -> ToolGateway:
    gate = PolicyGate(env_manager=env_manager, storage=adapter)
    return ToolGateway(policy_gate=gate)


def _make_context(symbiote_id: str, user_input: str, tools: list[dict] | None = None) -> AssembledContext:
    return AssembledContext(
        symbiote_id=symbiote_id,
        session_id="sess-1",
        user_input=user_input,
        available_tools=tools or [],
    )


class TestChatRunnerNoTools:
    def test_plain_response(self, symbiote_id: str) -> None:
        llm = MockLLM("Hello! How can I help?")
        runner = ChatRunner(llm)
        context = _make_context(symbiote_id, "Hi")
        result = runner.run(context)
        assert result.success is True
        assert result.output == "Hello! How can I help?"

    def test_response_with_no_gateway_ignores_tool_calls(self, symbiote_id: str) -> None:
        llm = MockLLM(
            "Let me check.\n\n"
            "```tool_call\n"
            '{"tool": "search", "params": {"q": "test"}}\n'
            "```\n\n"
            "Found it."
        )
        runner = ChatRunner(llm)  # no tool_gateway
        context = _make_context(symbiote_id, "search for test")
        result = runner.run(context)
        assert result.success is True
        # Without gateway, tool calls are parsed but not executed; text is cleaned
        assert "Let me check." in result.output
        assert "Found it." in result.output
        assert "tool_call" not in result.output


class TestChatRunnerWithTools:
    def test_tool_calls_are_executed(
        self,
        symbiote_id: str,
        tool_gateway: ToolGateway,
        env_manager: EnvironmentManager,
    ) -> None:
        # Register a tool
        tool_gateway.register_tool("add", lambda p: p.get("a", 0) + p.get("b", 0))
        env_manager.configure(symbiote_id=symbiote_id, tools=["add"])

        llm = MockLLM(
            "I'll add those.\n\n"
            "```tool_call\n"
            '{"tool": "add", "params": {"a": 2, "b": 3}}\n'
            "```\n\n"
            "Done."
        )
        runner = ChatRunner(llm, tool_gateway=tool_gateway)
        context = _make_context(symbiote_id, "add 2 + 3")
        result = runner.run(context)

        assert result.success is True
        assert isinstance(result.output, dict)
        assert "I'll add those." in result.output["text"]
        assert "Done." in result.output["text"]
        assert "tool_call" not in result.output["text"]
        assert len(result.output["tool_results"]) == 1
        assert result.output["tool_results"][0]["success"] is True
        assert result.output["tool_results"][0]["output"] == 5

    def test_tool_instructions_in_system_prompt(self, symbiote_id: str) -> None:
        messages_seen: list[list[dict]] = []

        class CaptureLLM:
            def complete(self, messages: list[dict], config: dict | None = None) -> str:
                messages_seen.append(messages)
                return "ok"

        runner = ChatRunner(CaptureLLM())
        tools = [
            {"tool_id": "search", "name": "Search", "description": "Search items", "parameters": {}},
        ]
        context = _make_context(symbiote_id, "find news", tools=tools)
        runner.run(context)

        assert len(messages_seen) == 1
        system = messages_seen[0][0]["content"]
        assert "Available Tools" in system
        assert "search" in system
        assert "tool_call" in system


class TestChatRunnerContextAssembly:
    def test_available_tools_field_in_context(self) -> None:
        ctx = AssembledContext(
            symbiote_id="s1",
            session_id="sess-1",
            user_input="test",
            available_tools=[
                {"tool_id": "t1", "name": "Tool1", "description": "Desc", "parameters": {}},
            ],
        )
        assert len(ctx.available_tools) == 1
        assert ctx.available_tools[0]["tool_id"] == "t1"
