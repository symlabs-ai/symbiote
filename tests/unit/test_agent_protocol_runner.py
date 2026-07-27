"""Offline integration contracts for AgentProtocol runner wiring."""

from __future__ import annotations

import copy
import logging
from pathlib import Path

import pytest

from symbiote.agent_protocols import AgentProtocolError
from symbiote.config.models import KernelConfig
from symbiote.core.context import AssembledContext
from symbiote.core.kernel import SymbioteKernel
from symbiote.environment.descriptors import (
    LLMResponse,
    NativeToolCall,
    ToolCall,
    ToolCallResult,
)
from symbiote.memory.working import WorkingMemory
from symbiote.runners.chat import ChatRunner


class CaptureLLM:
    def __init__(self, responses: list[str | LLMResponse]) -> None:
        self._responses = responses
        self.calls: list[tuple[list[dict], dict]] = []

    def complete(self, messages: list[dict], **kwargs):
        self.calls.append((copy.deepcopy(messages), copy.deepcopy(kwargs)))
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[index]


class TrackingGateway:
    def __init__(
        self,
        *,
        result_limit: int | None = None,
        risk_level: str = "low",
    ) -> None:
        self.result_limit = result_limit
        self.risk_level = risk_level
        self.execute_count = 0

    def execute_tool_calls(
        self,
        *,
        calls: list[ToolCall],
        **kwargs,
    ) -> list[ToolCallResult]:
        self.execute_count += 1
        selected = (
            calls
            if self.result_limit is None
            else calls[:self.result_limit]
        )
        return [
            ToolCallResult(
                tool_id=call.tool_id,
                success=True,
                output={"ok": True},
            )
            for call in selected
        ]

    def get_risk_level(self, tool_id: str) -> str:
        del tool_id
        return self.risk_level


def _context(
    *,
    tool_loading: str = "full",
    tool_mode: str = "brief",
    generation_settings: dict | None = None,
) -> AssembledContext:
    return AssembledContext(
        symbiote_id="sym-1",
        session_id="session-1",
        user_input="run lookup",
        tool_loading=tool_loading,
        tool_mode=tool_mode,
        generation_settings=generation_settings,
        available_tools=[
            {
                "tool_id": "lookup",
                "name": "Lookup",
                "description": "Look up a value",
                "parameters": {},
            }
        ],
    )


def _native_call(
    call_id: str,
    *,
    reasoning: str | None = None,
) -> LLMResponse:
    return LLMResponse(
        reasoning_content=reasoning,
        reasoning_state={"signature": call_id} if reasoning else None,
        tool_calls=[
            NativeToolCall(
                call_id=call_id,
                tool_id="lookup",
                params={"q": "value"},
            )
        ],
    )


class TestChatRunnerProtocolIntegration:
    def test_default_remains_legacy_without_host_migration(self) -> None:
        llm = CaptureLLM(["done"])
        runner = ChatRunner(llm)

        result = runner.run(
            _context(generation_settings={"temperature": 0.4}),
            llm_config={"max_tokens": 111},
        )

        assert result.success
        assert runner.agent_protocol_id == "legacy_text_v1"
        messages, kwargs = llm.calls[0]
        assert kwargs == {
            "config": {"temperature": 0.4, "max_tokens": 111}
        }
        assert "Available Tools" in messages[0]["content"]

    def test_laguna_forces_native_full_schemas_and_host_overrides(self) -> None:
        llm = CaptureLLM([LLMResponse(content="done")])
        runner = ChatRunner(
            llm,
            native_tools=False,
            agent_protocol="poolside_laguna_v1",
        )

        result = runner.run(
            _context(generation_settings={"temperature": 0.4}),
            llm_config={
                "agent_profile": "simple",
                "temperature": 0.2,
                "max_tokens": 333,
                "extra": {
                    "parallel_tool_calls": True,
                },
            },
        )

        assert result.success
        assert runner._native_tools is False
        messages, kwargs = llm.calls[0]
        assert "Available Tools" not in messages[0]["content"]
        assert "tool_call" not in messages[0]["content"]
        assert kwargs["tools"][0]["function"]["parameters"] == {
            "type": "object",
            "properties": {},
        }
        assert kwargs["config"]["temperature"] == 0.2
        assert kwargs["config"]["max_tokens"] == 333
        assert kwargs["config"]["extra"] == {
            "chat_template_kwargs": {"enable_thinking": False},
        }
        assert kwargs["config"]["parallel_tool_calls"] is False
        assert "agent_profile" not in kwargs["config"]

    def test_laguna_rejects_arbitrary_extra_before_llm(self) -> None:
        llm = CaptureLLM([LLMResponse(content="unused")])
        runner = ChatRunner(
            llm,
            agent_protocol="poolside_laguna_v1",
        )

        with pytest.raises(AgentProtocolError, match="unsupported keys"):
            runner.run(
                _context(),
                llm_config={"extra": {"trace_id": "host"}},
            )

        assert llm.calls == []

    def test_laguna_index_fails_before_calling_llm(self) -> None:
        llm = CaptureLLM([LLMResponse(content="unused")])
        runner = ChatRunner(
            llm,
            native_tools=True,
            agent_protocol="poolside_laguna_v1",
        )

        with pytest.raises(AgentProtocolError, match="complete tool schemas"):
            runner.run(_context(tool_loading="index"))

        assert llm.calls == []

    def test_laguna_rejects_text_tool_call_before_tool_side_effects(
        self,
    ) -> None:
        llm = CaptureLLM([
            (
                "```tool_call\n"
                '{"tool":"lookup","params":{"q":"value"}}\n'
                "```"
            )
        ])
        gateway = TrackingGateway(risk_level="high")
        approval_calls: list[str] = []
        after_calls: list[str] = []
        runner = ChatRunner(
            llm,
            tool_gateway=gateway,
            agent_protocol="poolside_laguna_v1",
            on_before_tool_call=lambda tool_id, params, risk: (
                approval_calls.append(tool_id) or True
            ),
            on_after_tool_result=lambda calls, results: (
                after_calls.append(calls[0].tool_id) or None
            ),
        )

        with pytest.raises(AgentProtocolError, match="native structured"):
            runner.run(_context())

        assert gateway.execute_count == 0
        assert approval_calls == []
        assert after_calls == []

    @pytest.mark.parametrize(
        ("native_calls", "error"),
        [
            (
                [
                    NativeToolCall(
                        call_id=None,
                        tool_id="lookup",
                        params={},
                    )
                ],
                "non-empty native call_id",
            ),
            (
                [
                    NativeToolCall(
                        call_id="duplicate",
                        tool_id="lookup",
                        params={},
                    ),
                    NativeToolCall(
                        call_id="duplicate",
                        tool_id="lookup",
                        params={},
                    ),
                ],
                "unique native call_id",
            ),
        ],
    )
    def test_laguna_rejects_ambiguous_ids_before_gateway(
        self,
        native_calls: list[NativeToolCall],
        error: str,
    ) -> None:
        gateway = TrackingGateway()
        runner = ChatRunner(
            CaptureLLM([LLMResponse(tool_calls=native_calls)]),
            tool_gateway=gateway,
            agent_protocol="poolside_laguna_v1",
        )

        with pytest.raises(AgentProtocolError, match=error):
            runner.run(_context())

        assert gateway.execute_count == 0

    def test_laguna_replays_reasoning_only_in_wire_memory(self) -> None:
        private_reasoning = "ephemeral laguna reasoning"
        llm = CaptureLLM([
            _native_call("call-laguna", reasoning=private_reasoning),
            LLMResponse(content="done"),
        ])
        working_memory = WorkingMemory("session-1")
        runner = ChatRunner(
            llm,
            working_memory=working_memory,
            tool_gateway=TrackingGateway(),
            agent_protocol="poolside_laguna_v1",
        )

        result = runner.run(_context())

        assert result.success
        next_messages = llm.calls[1][0]
        assistant = next(
            message
            for message in next_messages
            if message.get("role") == "assistant"
            and message.get("tool_calls")
        )
        assert assistant["reasoning_content"] == private_reasoning
        assert assistant["reasoning_state"] == {
            "signature": "call-laguna"
        }
        assert assistant["tool_calls"][0]["id"] == "call-laguna"
        tool_message = next(
            message
            for message in next_messages
            if message.get("role") == "tool"
        )
        assert tool_message["tool_call_id"] == "call-laguna"
        assert private_reasoning not in str(working_memory.snapshot())

    def test_gateway_cardinality_mismatch_fails_explicitly(self) -> None:
        llm = CaptureLLM([
            LLMResponse(
                tool_calls=[
                    NativeToolCall(
                        call_id="call-1",
                        tool_id="lookup",
                        params={},
                    ),
                    NativeToolCall(
                        call_id="call-2",
                        tool_id="lookup",
                        params={},
                    ),
                ]
            )
        ])
        gateway = TrackingGateway(result_limit=1)
        runner = ChatRunner(
            llm,
            tool_gateway=gateway,
            agent_protocol="poolside_laguna_v1",
        )

        with pytest.raises(
            ValueError,
            match="2 calls but 1 results",
        ):
            runner.run(_context())


class TestProtocolAuditAndLogging:
    def test_instant_result_has_safe_audit_without_tool_call(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(
            logging.INFO,
            logger="symbiote.runners.chat",
        )
        runner = ChatRunner(
            CaptureLLM([LLMResponse(content="done")]),
            agent_protocol="poolside_laguna_v1",
        )

        result = runner.run(_context(tool_mode="instant"))

        assert result.agent_protocol is not None
        assert result.agent_protocol.model_dump() == {
            "protocol_id": "poolside_laguna_v1",
            "profile": "simple",
            "schema_strategy": "full",
            "native_tools": True,
            "exchange_count": 0,
        }
        audit_logs = [
            record.getMessage()
            for record in caplog.records
            if record.getMessage().startswith("agent_protocol_completed")
        ]
        assert len(audit_logs) == 1
        assert "protocol_id=poolside_laguna_v1" in audit_logs[0]
        assert "profile=simple" in audit_logs[0]
        assert "schema_strategy=full" in audit_logs[0]
        assert "native_tools=True" in audit_logs[0]
        assert "exchange_count=0" in audit_logs[0]

    def test_loop_audit_and_logs_exclude_sensitive_wire_data(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        reasoning_sentinel = "REASONING_SENTINEL_DO_NOT_PERSIST"
        params_sentinel = "PARAMS_SENTINEL_DO_NOT_LOG"
        output_sentinel = "OUTPUT_SENTINEL_DO_NOT_LOG"

        class SensitiveGateway(TrackingGateway):
            def execute_tool_calls(
                self,
                *,
                calls: list[ToolCall],
                **kwargs,
            ) -> list[ToolCallResult]:
                self.execute_count += 1
                return [
                    ToolCallResult(
                        call_id=call.call_id,
                        tool_id=call.tool_id,
                        success=True,
                        output={"secret": output_sentinel},
                    )
                    for call in calls
                ]

        response = LLMResponse(
            reasoning_content=reasoning_sentinel,
            reasoning_state={"secret": reasoning_sentinel},
            tool_calls=[
                NativeToolCall(
                    call_id="call-sensitive",
                    tool_id="lookup",
                    params={"secret": params_sentinel},
                )
            ],
        )
        memory = WorkingMemory("session-1")
        caplog.set_level(
            logging.INFO,
            logger="symbiote.runners.chat",
        )
        runner = ChatRunner(
            CaptureLLM([response, LLMResponse(content="done")]),
            working_memory=memory,
            tool_gateway=SensitiveGateway(),
            agent_protocol="poolside_laguna_v1",
        )

        result = runner.run(_context())

        assert result.agent_protocol is not None
        assert result.agent_protocol.model_dump() == {
            "protocol_id": "poolside_laguna_v1",
            "profile": "complex",
            "schema_strategy": "full",
            "native_tools": True,
            "exchange_count": 1,
        }
        logs = "\n".join(record.getMessage() for record in caplog.records)
        assert reasoning_sentinel not in logs
        assert params_sentinel not in logs
        assert output_sentinel not in logs
        assert sum(
            record.getMessage().startswith("agent_protocol_completed")
            for record in caplog.records
        ) == 1
        assert reasoning_sentinel not in str(result.model_dump())
        assert reasoning_sentinel not in str(memory.snapshot())
        assert params_sentinel not in str(memory.snapshot())
        assert output_sentinel not in str(memory.snapshot())

    def test_no_tools_still_returns_protocol_audit(self) -> None:
        context = _context()
        context.available_tools = []
        runner = ChatRunner(
            CaptureLLM([LLMResponse(content="done")]),
            agent_protocol="poolside_laguna_v1",
        )

        result = runner.run(context)

        assert result.loop_trace is None
        assert result.agent_protocol is not None
        assert result.agent_protocol.protocol_id == "poolside_laguna_v1"
        assert result.agent_protocol.profile == "complex"
        assert result.agent_protocol.schema_strategy == "full"
        assert result.agent_protocol.native_tools is True
        assert result.agent_protocol.exchange_count == 0

    def test_rejected_response_logs_only_safe_error_code_and_protocol(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        response_sentinel = "RESPONSE_SENTINEL_DO_NOT_LOG"
        gateway = TrackingGateway(risk_level="high")
        runner = ChatRunner(
            CaptureLLM([
                (
                    "```tool_call\n"
                    '{"tool":"lookup","params":{"secret":"'
                    + response_sentinel
                    + '"}}'
                    "\n```"
                )
            ]),
            tool_gateway=gateway,
            agent_protocol="poolside_laguna_v1",
        )
        caplog.set_level(
            logging.WARNING,
            logger="symbiote.runners.chat",
        )

        with pytest.raises(AgentProtocolError, match="native structured"):
            runner.run(_context())

        assert gateway.execute_count == 0
        messages = [record.getMessage() for record in caplog.records]
        assert messages == [
            (
                "agent_protocol_failed code=response_rejected "
                "protocol_id=poolside_laguna_v1"
            )
        ]
        assert response_sentinel not in messages[0]


class TestKernelProtocolInjection:
    def test_constructor_and_setter_select_exact_protocol(
        self,
        tmp_path: Path,
    ) -> None:
        kernel = SymbioteKernel(
            KernelConfig(db_path=tmp_path / "kernel.db"),
            llm=CaptureLLM(["done"]),
            agent_protocol="poolside_laguna_v1",
        )
        runner = kernel._runner_registry.select("chat")

        assert runner is not None
        assert runner.agent_protocol_id == "poolside_laguna_v1"

        kernel.set_agent_protocol(None)
        assert runner.agent_protocol_id == "legacy_text_v1"

        kernel.set_agent_protocol("poolside_laguna_v1")
        assert runner.agent_protocol_id == "poolside_laguna_v1"

    def test_setter_without_llm_fails_clearly(self, tmp_path: Path) -> None:
        kernel = SymbioteKernel(
            KernelConfig(db_path=tmp_path / "kernel-no-llm.db"),
        )

        with pytest.raises(RuntimeError, match="without an LLM"):
            kernel.set_agent_protocol("poolside_laguna_v1")

    def test_config_selects_poolside_when_constructor_override_omitted(
        self,
        tmp_path: Path,
    ) -> None:
        kernel = SymbioteKernel(
            KernelConfig(
                db_path=tmp_path / "kernel-configured.db",
                agent_protocol="poolside_laguna_v1",
            ),
            llm=CaptureLLM([LLMResponse(content="done")]),
        )
        runner = kernel._runner_registry.select("chat")

        assert runner is not None
        assert runner.agent_protocol_id == "poolside_laguna_v1"

    def test_explicit_legacy_id_overrides_config_for_emergency_rollback(
        self,
        tmp_path: Path,
    ) -> None:
        kernel = SymbioteKernel(
            KernelConfig(
                db_path=tmp_path / "kernel-rollback.db",
                agent_protocol="poolside_laguna_v1",
            ),
            llm=CaptureLLM(["done"]),
            agent_protocol="legacy_text_v1",
        )
        runner = kernel._runner_registry.select("chat")

        assert runner is not None
        assert runner.agent_protocol_id == "legacy_text_v1"

    def test_unknown_configured_protocol_fails_before_storage_init(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "must-not-exist.db"

        with pytest.raises(AgentProtocolError, match="unknown agent protocol"):
            SymbioteKernel(
                KernelConfig(
                    db_path=db_path,
                    agent_protocol="unknown_protocol_v1",
                ),
                llm=CaptureLLM(["unused"]),
            )

        assert not db_path.exists()
