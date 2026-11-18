"""Contract tests for canonical native tool-call history."""

from __future__ import annotations

import asyncio
import copy
import logging
from types import SimpleNamespace

import pytest
from forge_llm import ChatMessage

from symbiote.adapters.llm.forge import ForgeLLMAdapter
from symbiote.agent_protocols.base import flatten_tool_exchanges
from symbiote.core.context import AssembledContext
from symbiote.environment.descriptors import (
    LLMResponse,
    NativeToolCall,
    ToolCall,
    ToolCallResult,
)
from symbiote.memory.working import WorkingMemory
from symbiote.runners.chat import ChatRunner


class SequenceLLM:
    def __init__(self, responses: list[str | LLMResponse]) -> None:
        self.responses = responses
        self.calls: list[list[dict]] = []
        self.tools: list[list[dict] | None] = []
        self._index = 0

    def complete(self, messages: list[dict], **kwargs):
        self.calls.append(copy.deepcopy(messages))
        self.tools.append(kwargs.get("tools"))
        response = self.responses[min(self._index, len(self.responses) - 1)]
        self._index += 1
        return response


class ResultGateway:
    def __init__(self, *, success: bool = True, output: object = None) -> None:
        self.success = success
        self.output = {"ok": True} if output is None else output

    def execute_tool_calls(self, *, calls: list[ToolCall], **kwargs):
        return [
            ToolCallResult(
                tool_id=call.tool_id,
                success=self.success,
                output=self.output if self.success else None,
                error=None if self.success else "failed",
            )
            for call in calls
        ]

    async def execute_tool_calls_async(
        self,
        *,
        calls: list[ToolCall],
        **kwargs,
    ):
        return self.execute_tool_calls(calls=calls, **kwargs)

    def get_risk_level(self, tool_id: str) -> str:
        return "low"


def _context(*, tool_mode: str = "brief") -> AssembledContext:
    return AssembledContext(
        symbiote_id="sym-1",
        session_id="session-1",
        user_input="execute",
        tool_mode=tool_mode,
        tool_loop=tool_mode != "instant",
        max_tool_iterations=5,
    )


def _native_call(
    call_id: str,
    *,
    reasoning: str = "ephemeral reasoning",
) -> LLMResponse:
    return LLMResponse(
        reasoning_content=reasoning,
        reasoning_state={"signature": f"state-{call_id}"},
        tool_calls=[
            NativeToolCall(
                call_id=call_id,
                tool_id="lookup",
                params={"q": "value"},
            )
        ],
    )


def _assert_native_exchange(
    messages: list[dict],
    *,
    call_id: str,
    reasoning: str,
    success: bool = True,
) -> None:
    assistant = next(
        message
        for message in messages
        if message.get("role") == "assistant"
        and message.get("tool_calls")
        and message["tool_calls"][0]["id"] == call_id
    )
    assert assistant["reasoning_content"] == reasoning
    assert assistant["reasoning_state"] == {"signature": f"state-{call_id}"}
    assert assistant["tool_calls"][0]["function"] == {
        "name": "lookup",
        "arguments": '{"q":"value"}',
    }

    tool = next(
        message
        for message in messages
        if message.get("role") == "tool" and message.get("tool_call_id") == call_id
    )
    assert tool["name"] == "lookup"
    expected_success = "true" if success else "false"
    assert f'"success":{expected_success}' in tool["content"]


class TestFlattenToolExchanges:
    def test_messages_without_tool_structures_are_returned_as_they_are(self) -> None:
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "a"},
        ]

        assert flatten_tool_exchanges(messages) == messages

    def test_parallel_results_become_one_user_message(self) -> None:
        messages = [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "checking both",
                "tool_calls": [
                    {"id": "a", "type": "function",
                     "function": {"name": "one", "arguments": '{"x":1}'}},
                    {"id": "b", "type": "function",
                     "function": {"name": "two", "arguments": ""}},
                ],
            },
            {"role": "tool", "tool_call_id": "a", "name": "one", "content": "r1"},
            {"role": "tool", "tool_call_id": "b", "content": "r2"},
            {"role": "user", "content": "stop"},
        ]
        original = copy.deepcopy(messages)

        flat = flatten_tool_exchanges(messages)

        assert flat == [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": 'checking both\n[Called tool: one]\n{"x":1}\n[Called tool: two]\n{}',
            },
            {"role": "user", "content": "[Tool result: one]\nr1\n\n[Tool result: b]\nr2"},
            {"role": "user", "content": "stop"},
        ]
        assert messages == original  # the loop's own list is not touched


class TestNativeHistory:
    def test_empty_tool_result_is_non_empty_on_provider_retry(self) -> None:
        class ProviderRulesLLM(SequenceLLM):
            def complete(self, messages: list[dict], **kwargs):
                for message in messages:
                    content = message.get("content")
                    if (
                        message.get("role") == "tool"
                        and isinstance(content, str)
                        and not content.strip()
                    ):
                        raise ValueError(
                            "tool result text content blocks must be non-empty"
                        )
                return super().complete(messages, **kwargs)

        llm = ProviderRulesLLM(
            [_native_call("empty-result"), LLMResponse(content="complete")]
        )
        runner = ChatRunner(
            llm,
            tool_gateway=ResultGateway(output=""),
            native_tools=True,
        )

        result = runner.run(_context())

        assert result.success
        tool_message = next(
            message for message in llm.calls[1] if message.get("role") == "tool"
        )
        assert tool_message["content"].strip()
        assert "(no output)" in tool_message["content"]

    def test_sync_loop_replays_native_turn_and_keeps_reasoning_ephemeral(
        self,
        caplog,
    ) -> None:
        reasoning = "never persist this reasoning"
        llm = SequenceLLM(
            [
                _native_call("call-sync", reasoning=reasoning),
                LLMResponse(content="complete"),
            ]
        )
        working_memory = WorkingMemory("session-1")
        runner = ChatRunner(
            llm,
            working_memory=working_memory,
            tool_gateway=ResultGateway(),
            native_tools=True,
        )

        with caplog.at_level(logging.DEBUG):
            result = runner.run(_context())

        assert result.success
        _assert_native_exchange(
            llm.calls[1],
            call_id="call-sync",
            reasoning=reasoning,
        )
        assert reasoning not in str(working_memory.snapshot())
        assert reasoning not in caplog.text

    def test_async_loop_replays_native_turn(self) -> None:
        llm = SequenceLLM(
            [
                _native_call("call-async"),
                LLMResponse(content="complete"),
            ]
        )
        runner = ChatRunner(
            llm,
            tool_gateway=ResultGateway(),
            native_tools=True,
        )

        result = asyncio.run(runner.run_async(_context()))

        assert result.success
        _assert_native_exchange(
            llm.calls[1],
            call_id="call-async",
            reasoning="ephemeral reasoning",
        )

    def test_instant_failure_round_trip_is_native(self) -> None:
        llm = SequenceLLM(
            [
                _native_call("call-instant"),
                LLMResponse(content="tool failed safely"),
            ]
        )
        runner = ChatRunner(
            llm,
            tool_gateway=ResultGateway(success=False),
            native_tools=True,
        )

        result = runner.run(_context(tool_mode="instant"))

        assert result.success
        assert len(llm.calls) == 2
        _assert_native_exchange(
            llm.calls[1],
            call_id="call-instant",
            reasoning="ephemeral reasoning",
            success=False,
        )
        tool_message = next(
            message for message in llm.calls[1] if message.get("role") == "tool"
        )
        assert '"success":false' in tool_message["content"]

    @pytest.mark.parametrize("use_async", [False, True])
    def test_stagnation_injection_receives_the_exchanges_as_text(
        self, use_async: bool
    ) -> None:
        """The forced final answer is asked without ``tools``, so the request
        carries no tool-call or tool-result structure either: providers refuse
        those when no tool is declared. Nothing of the exchanges is lost."""
        llm = SequenceLLM(
            [
                _native_call("call-one", reasoning="reason-one"),
                _native_call("call-two", reasoning="reason-two"),
                "stopped",
            ]
        )
        runner = ChatRunner(
            llm,
            tool_gateway=ResultGateway(),
            native_tools=True,
        )
        context = _context()
        context.available_tools = [
            {
                "tool_id": "lookup",
                "name": "lookup",
                "description": "look something up",
                "parameters": {"type": "object", "properties": {}},
            }
        ]

        if use_async:
            result = asyncio.run(runner.run_async(context))
        else:
            result = runner.run(context)

        assert result.success
        assert len(llm.calls) == 3
        # The loop calls declare the tools and replay the native exchange...
        assert llm.tools[0] and llm.tools[1]
        _assert_native_exchange(llm.calls[1], call_id="call-one", reasoning="reason-one")
        # ...the final one declares none and carries none.
        assert llm.tools[2] is None
        injection_messages = llm.calls[2]
        assert not any("tool_calls" in m or "tool_call_id" in m for m in injection_messages)
        assert {m["role"] for m in injection_messages} <= {"system", "user", "assistant"}

        assistants = [m for m in injection_messages if m["role"] == "assistant"]
        assert [m["reasoning_content"] for m in assistants] == ["reason-one", "reason-two"]
        assert [m["reasoning_state"] for m in assistants] == [
            {"signature": "state-call-one"},
            {"signature": "state-call-two"},
        ]
        for message in assistants:
            assert message["content"] == '[Called tool: lookup]\n{"q":"value"}'
        results = [
            m for m in injection_messages
            if m["role"] == "user" and m["content"].startswith("[Tool result: lookup]")
        ]
        assert len(results) == 2
        assert all('"success":true' in m["content"] for m in results)

        assert injection_messages[-1]["role"] == "user"
        assert "repeating the same action" in injection_messages[-1]["content"]

    def test_string_response_keeps_legacy_text_exchange(self) -> None:
        llm = SequenceLLM(
            [
                '```tool_call\n{"tool":"lookup","params":{"q":"value"}}\n```',
                "complete",
            ]
        )
        runner = ChatRunner(
            llm,
            tool_gateway=ResultGateway(),
            native_tools=True,
        )

        result = runner.run(_context())

        assert result.success
        assistant, result_message = llm.calls[1][-2:]
        assert assistant["role"] == "assistant"
        assert "```tool_call" in assistant["content"]
        assert "tool_calls" not in assistant
        assert result_message["role"] == "user"
        assert "[Tool result: lookup]" in result_message["content"]


class TestNativeCorrelationAndCompaction:
    def test_call_ids_survive_cache_approval_and_merge(self) -> None:
        runner = ChatRunner(
            SequenceLLM(["done"]),
            tool_gateway=ResultGateway(),
            on_before_tool_call=lambda tool_id, params, risk: False,
        )
        calls = [
            ToolCall(
                call_id="call-cache",
                tool_id="get_tool_schema",
                params={"tool_id": "lookup"},
            ),
            ToolCall(call_id="call-denied", tool_id="danger", params={}),
            ToolCall(call_id="call-exec", tool_id="lookup", params={}),
        ]

        cached, remaining = runner._split_cached_schema_calls(
            calls,
            {"lookup": {"type": "object"}},
            "index",
        )
        assert cached[0].call_id == "call-cache"
        assert [call.call_id for call in remaining] == [
            "call-denied",
            "call-exec",
        ]

        class ApprovalGateway(ResultGateway):
            def get_risk_level(self, tool_id: str) -> str:
                return "high" if tool_id == "danger" else "low"

        runner._tool_gateway = ApprovalGateway()
        approved, denied = runner._check_approval(remaining)
        assert [call.call_id for call in approved] == ["call-exec"]
        assert denied[0].call_id == "call-denied"

        remaining_results = runner._merge_tool_results(
            remaining,
            denied,
            [ToolCallResult(tool_id="lookup", success=True)],
        )
        merged = runner._merge_tool_results(
            calls,
            cached,
            remaining_results,
        )
        assert [result.call_id for result in merged] == [
            "call-cache",
            "call-denied",
            "call-exec",
        ]

    def test_compaction_keeps_last_native_exchange_complete(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "request"},
        ]
        initial_count = len(messages)
        for index in range(4):
            messages.extend(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": f"reason-{index}",
                        "tool_calls": [
                            {
                                "id": f"call-{index}",
                                "type": "function",
                                "function": {
                                    "name": "lookup",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": f"call-{index}",
                        "name": "lookup",
                        "content": '{"success":true}',
                    },
                ]
            )

        ChatRunner._compact_loop_messages(messages, initial_count)

        assert messages[initial_count]["role"] == "user"
        assert "compacted" in messages[initial_count]["content"].lower()
        assert messages[-2]["role"] == "assistant"
        assert messages[-2]["tool_calls"][0]["id"] == "call-3"
        assert messages[-1]["role"] == "tool"
        assert messages[-1]["tool_call_id"] == "call-3"

    def test_autocompact_accepts_native_tool_messages(self) -> None:
        reasoning = "large private reasoning " * 30

        def exchange(call_id: str, name: str, thought: str) -> list[dict]:
            return [
                {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": thought,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": '{"success":true}',
                },
            ]

        runner = ChatRunner(SequenceLLM(["done"]), context_budget=20)
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "request"},
            *exchange("call-1", "lookup", reasoning),
            *exchange("call-2", "fetch", "latest thought"),
        ]

        assert runner._autocompact_if_needed(messages, 2) is True
        # older native exchange summarized (its reasoning dropped) ...
        assert messages[2]["role"] == "user"
        assert "lookup" in messages[2]["content"]
        assert reasoning not in str(messages)
        # ... latest assistant + tool pair kept intact, never orphaned
        assert [m["role"] for m in messages[3:]] == ["assistant", "tool"]
        assert messages[-1]["tool_call_id"] == "call-2"
        assert messages[3]["reasoning_content"] == "latest thought"


class TestForgeCanonicalBridge:
    def test_outgoing_native_fields_are_not_flattened(self) -> None:
        captured: list[list[ChatMessage]] = []

        class Agent:
            def chat(self, *, messages, **kwargs):
                captured.append(messages)
                return SimpleNamespace(
                    content="done",
                    message=SimpleNamespace(tool_calls=None),
                    metadata=SimpleNamespace(
                        model="model-a",
                        provider="provider-a",
                        finish_reason="stop",
                        raw_response=None,
                    ),
                    token_usage=None,
                )

        adapter = object.__new__(ForgeLLMAdapter)
        adapter._agent = Agent()
        result = adapter.complete(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "wire-only",
                    "reasoning_state": {"signature": "state"},
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "content": '{"success":true}',
                    "name": "lookup",
                    "tool_call_id": "call-1",
                },
            ]
        )

        assert result == "done"
        assistant = captured[0][0]
        tool = captured[0][1]
        assert type(assistant) is ChatMessage
        assert type(tool) is ChatMessage
        assert "reasoning_content" not in assistant.to_dict()
        assert assistant.to_wire_dict()["reasoning_content"] == "wire-only"
        assert assistant.to_wire_dict()["reasoning_state"] == {
            "signature": "state"
        }
        assert assistant.tool_calls is not None
        assert assistant.tool_calls[0]["id"] == "call-1"
        assert tool.tool_call_id == "call-1"

    def test_official_chat_message_wire_round_trip_preserves_ephemeral_data(
        self,
    ) -> None:
        original = ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call-round-trip",
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "arguments": '{"q":"value"}',
                    },
                }
            ],
            reasoning_content="private",
            reasoning_state={"signature": "opaque"},
        )

        restored = ChatMessage.from_wire_dict(original.to_wire_dict())

        assert type(restored) is ChatMessage
        assert restored.reasoning_content == "private"
        assert restored.reasoning_state == {"signature": "opaque"}
        assert restored.tool_calls is not None
        assert restored.tool_calls[0]["id"] == "call-round-trip"
        assert "reasoning_content" not in restored.to_dict()

    def test_native_response_maps_reasoning_and_envelope(self) -> None:
        class Agent:
            def chat(self, **kwargs):
                return SimpleNamespace(
                    content="",
                    message=SimpleNamespace(
                        reasoning_content="private",
                        reasoning_state={"signature": "opaque"},
                        tool_calls=[
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "lookup",
                                    "arguments": '{"q":"value"}',
                                },
                            }
                        ],
                    ),
                    metadata=SimpleNamespace(
                        model="model-a",
                        provider="provider-a",
                        finish_reason="tool_calls",
                        raw_response=None,
                        request_id="req-1",
                    ),
                    token_usage=SimpleNamespace(
                        prompt_tokens=12,
                        completion_tokens=3,
                        total_tokens=15,
                    ),
                )

        adapter = object.__new__(ForgeLLMAdapter)
        adapter._agent = Agent()
        result = adapter.complete([{"role": "user", "content": "go"}])

        assert isinstance(result, LLMResponse)
        assert result.reasoning_content == "private"
        assert result.reasoning_state == {"signature": "opaque"}
        assert result.finish_reason == "tool_calls"
        assert result.tool_calls[0].call_id == "call-1"
        assert result.model_identity is not None
        assert result.model_identity.model == "model-a"
        assert result.usage is not None
        assert result.usage.total_tokens == 15
        assert result.metadata == {"request_id": "req-1"}
