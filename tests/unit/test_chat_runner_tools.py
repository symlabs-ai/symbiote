"""Tests for ChatRunner with tool execution."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.context import AssembledContext
from symbiote.core.identity import IdentityManager
from symbiote.core.ports import LLMPort
from symbiote.environment.descriptors import (
    LLMResponse,
    NativeToolCall,
    ToolDescriptor,
)
from symbiote.environment.manager import EnvironmentManager
from symbiote.environment.policies import PolicyGate
from symbiote.environment.tools import ToolGateway
from symbiote.runners.chat import ChatRunner


class MockLLM:
    """Mock LLM that returns a predetermined response."""

    def __init__(self, response: str) -> None:
        self._response = response

    def complete(self, messages: list[dict], config: dict | None = None, tools: list[dict] | None = None) -> str:
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
            def complete(self, messages: list[dict], config: dict | None = None, tools: list[dict] | None = None) -> str:
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


# ── Native function calling tests ──────────────────────────────────────────


class NativeMockLLM:
    """Mock LLM that returns LLMResponse with native tool calls."""

    def __init__(self, response: LLMResponse) -> None:
        self._response = response
        self.last_tools: list[dict] | None = None
        self.last_messages: list[dict] | None = None

    def complete(
        self,
        messages: list[dict],
        config: dict | None = None,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        self.last_tools = tools
        self.last_messages = messages
        return self._response


class TestChatRunnerNativeTools:
    def test_native_tool_calls_executed(
        self,
        symbiote_id: str,
        tool_gateway: ToolGateway,
        env_manager: EnvironmentManager,
    ) -> None:
        """Native tool calls from LLMResponse are executed via the gateway."""
        tool_gateway.register_tool("add", lambda p: p.get("a", 0) + p.get("b", 0))
        env_manager.configure(symbiote_id=symbiote_id, tools=["add"])

        llm = NativeMockLLM(
            LLMResponse(
                content="I'll add those for you.",
                tool_calls=[
                    NativeToolCall(call_id="c1", tool_id="add", params={"a": 2, "b": 3}),
                ],
            )
        )
        runner = ChatRunner(llm, tool_gateway=tool_gateway, native_tools=True)
        context = _make_context(symbiote_id, "add 2 + 3")
        result = runner.run(context)

        assert result.success is True
        assert isinstance(result.output, dict)
        assert "I'll add those for you." in result.output["text"]
        assert len(result.output["tool_results"]) == 1
        assert result.output["tool_results"][0]["success"] is True
        assert result.output["tool_results"][0]["output"] == 5

    def test_native_tools_passes_tool_defs_to_llm(self, symbiote_id: str) -> None:
        """When native_tools=True, tool definitions are passed to complete()."""
        llm = NativeMockLLM(LLMResponse(content="ok"))
        runner = ChatRunner(llm, native_tools=True)
        tools = [
            {"tool_id": "search", "name": "Search", "description": "Search items", "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            }},
        ]
        context = _make_context(symbiote_id, "find news", tools=tools)
        runner.run(context)

        assert llm.last_tools is not None
        assert len(llm.last_tools) == 1
        assert llm.last_tools[0]["type"] == "function"
        assert llm.last_tools[0]["function"]["name"] == "search"

    def test_native_tools_omits_text_instructions(self, symbiote_id: str) -> None:
        """When native_tools=True, text-based tool instructions are NOT in system prompt."""
        llm = NativeMockLLM(LLMResponse(content="ok"))
        runner = ChatRunner(llm, native_tools=True)
        tools = [
            {"tool_id": "search", "name": "Search", "description": "Search items", "parameters": {}},
        ]
        context = _make_context(symbiote_id, "find news", tools=tools)
        runner.run(context)

        system_msg = llm.last_messages[0]["content"]
        assert "tool_call" not in system_msg
        assert "Available Tools" not in system_msg

    def test_llm_response_without_tool_calls(self, symbiote_id: str) -> None:
        """LLMResponse with no tool_calls returns clean text output."""
        llm = NativeMockLLM(LLMResponse(content="Just a text response."))
        runner = ChatRunner(llm, native_tools=True)
        context = _make_context(symbiote_id, "hello")
        result = runner.run(context)

        assert result.success is True
        assert result.output == "Just a text response."

    def test_backward_compat_str_response_with_native_flag(self, symbiote_id: str) -> None:
        """Even with native_tools=True, a str response falls back to text-based parsing."""
        llm = MockLLM("Plain text response.")
        runner = ChatRunner(llm, native_tools=True)
        context = _make_context(symbiote_id, "hello")
        result = runner.run(context)

        assert result.success is True
        assert result.output == "Plain text response."

    def test_native_no_tools_in_context_means_no_defs(self, symbiote_id: str) -> None:
        """When context has no tools, native_tool_defs stays None."""
        llm = NativeMockLLM(LLMResponse(content="ok"))
        runner = ChatRunner(llm, native_tools=True)
        context = _make_context(symbiote_id, "hello")
        runner.run(context)

        assert llm.last_tools is None
