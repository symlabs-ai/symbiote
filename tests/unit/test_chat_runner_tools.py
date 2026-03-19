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


def _make_context(
    symbiote_id: str, user_input: str,
    tools: list[dict] | None = None,
    tool_loop: bool = False,
) -> AssembledContext:
    return AssembledContext(
        symbiote_id=symbiote_id,
        session_id="sess-1",
        user_input=user_input,
        available_tools=tools or [],
        tool_loop=tool_loop,
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


# ══════════════════════════════════════════════════════════════════════════
# TOOL LOOP TESTS
# ══════════════════════════════════════════════════════════════════════════


class MultiStepMockLLM:
    """Mock LLM that returns different responses on each call.

    First call returns a tool call, second call returns a final text.
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self._call_idx = 0
        self.call_count = 0
        self.all_messages: list[list[dict]] = []

    def complete(self, messages, **kwargs):
        self.call_count += 1
        self.all_messages.append(list(messages))
        idx = min(self._call_idx, len(self._responses) - 1)
        self._call_idx += 1
        return self._responses[idx]


class TestToolLoop:
    """Tests for the tool execution loop (Ralph Loop)."""

    def test_loop_completes_multi_step_task(
        self, symbiote_id, tool_gateway, env_manager,
    ) -> None:
        """Loop feeds tool results back to LLM until it responds without tools."""
        tool_gateway.register_tool("items_list", lambda p: [{"id": 42, "title": "Incêndio"}])
        tool_gateway.register_tool("items_publish", lambda p: {"published": True})
        env_manager.configure(symbiote_id=symbiote_id, tools=["items_list", "items_publish"])

        llm = MultiStepMockLLM([
            # Step 1: LLM calls items_list
            '```tool_call\n{"tool": "items_list", "params": {}}\n```',
            # Step 2: LLM sees results, calls items_publish
            '```tool_call\n{"tool": "items_publish", "params": {"item_id": 42}}\n```',
            # Step 3: LLM sees publish result, responds with text
            "Matéria publicada com sucesso.",
        ])
        runner = ChatRunner(llm, tool_gateway=tool_gateway)
        context = _make_context(symbiote_id, "publique a matéria", tool_loop=True)
        result = runner.run(context)

        assert result.success is True
        assert llm.call_count == 3
        assert isinstance(result.output, dict)
        assert result.output["text"] == "Matéria publicada com sucesso."
        assert len(result.output["tool_results"]) == 2
        assert result.output["tool_results"][0]["tool_id"] == "items_list"
        assert result.output["tool_results"][1]["tool_id"] == "items_publish"

    def test_loop_disabled_is_single_shot(
        self, symbiote_id, tool_gateway, env_manager,
    ) -> None:
        """With tool_loop=False, only one LLM call is made."""
        tool_gateway.register_tool("items_list", lambda p: [{"id": 42}])
        env_manager.configure(symbiote_id=symbiote_id, tools=["items_list"])

        llm = MultiStepMockLLM([
            '```tool_call\n{"tool": "items_list", "params": {}}\n```',
            "Should never reach this.",
        ])
        runner = ChatRunner(llm, tool_gateway=tool_gateway)
        context = _make_context(symbiote_id, "list items", tool_loop=False)
        result = runner.run(context)

        assert result.success is True
        assert llm.call_count == 1
        assert isinstance(result.output, dict)
        assert len(result.output["tool_results"]) == 1

    def test_loop_stops_when_no_tool_calls(self, symbiote_id) -> None:
        """Loop stops immediately when LLM responds without tool calls."""
        llm = MultiStepMockLLM(["Just a plain answer."])
        runner = ChatRunner(llm)
        context = _make_context(symbiote_id, "hello", tool_loop=True)
        result = runner.run(context)

        assert result.success is True
        assert llm.call_count == 1
        assert result.output == "Just a plain answer."

    def test_loop_respects_max_iterations(
        self, symbiote_id, tool_gateway, env_manager,
    ) -> None:
        """Loop stops at _MAX_TOOL_ITERATIONS even if LLM keeps calling tools."""
        tool_gateway.register_tool("echo", lambda p: "ok")
        env_manager.configure(symbiote_id=symbiote_id, tools=["echo"])

        # LLM always returns a tool call
        infinite_response = '```tool_call\n{"tool": "echo", "params": {}}\n```'
        llm = MultiStepMockLLM([infinite_response] * 20)
        runner = ChatRunner(llm, tool_gateway=tool_gateway)
        context = _make_context(symbiote_id, "loop forever", tool_loop=True)
        result = runner.run(context)

        assert result.success is True
        from symbiote.runners.chat import _MAX_TOOL_ITERATIONS
        assert llm.call_count == _MAX_TOOL_ITERATIONS

    def test_loop_feeds_results_in_messages(
        self, symbiote_id, tool_gateway, env_manager,
    ) -> None:
        """Verify that tool results appear in messages sent to the LLM."""
        tool_gateway.register_tool("get_name", lambda p: "Alice")
        env_manager.configure(symbiote_id=symbiote_id, tools=["get_name"])

        llm = MultiStepMockLLM([
            '```tool_call\n{"tool": "get_name", "params": {}}\n```',
            "Hello Alice!",
        ])
        runner = ChatRunner(llm, tool_gateway=tool_gateway)
        context = _make_context(symbiote_id, "who am I?", tool_loop=True)
        runner.run(context)

        # Second call should include tool result in messages
        second_call_messages = llm.all_messages[1]
        tool_result_msg = second_call_messages[-1]  # last message = tool result
        assert "[Tool result: get_name]" in tool_result_msg["content"]
        assert "Alice" in tool_result_msg["content"]

    def test_loop_accumulates_all_tool_results(
        self, symbiote_id, tool_gateway, env_manager,
    ) -> None:
        """All tool results across iterations are accumulated in output."""
        tool_gateway.register_tool("step1", lambda p: "result1")
        tool_gateway.register_tool("step2", lambda p: "result2")
        tool_gateway.register_tool("step3", lambda p: "result3")
        env_manager.configure(symbiote_id=symbiote_id, tools=["step1", "step2", "step3"])

        llm = MultiStepMockLLM([
            '```tool_call\n{"tool": "step1", "params": {}}\n```',
            '```tool_call\n{"tool": "step2", "params": {}}\n```',
            '```tool_call\n{"tool": "step3", "params": {}}\n```',
            "All done.",
        ])
        runner = ChatRunner(llm, tool_gateway=tool_gateway)
        context = _make_context(symbiote_id, "do 3 steps", tool_loop=True)
        result = runner.run(context)

        assert llm.call_count == 4
        assert len(result.output["tool_results"]) == 3
        tool_ids = [r["tool_id"] for r in result.output["tool_results"]]
        assert tool_ids == ["step1", "step2", "step3"]
