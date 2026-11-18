"""Offline contracts for versioned agent protocols."""

from __future__ import annotations

import pytest

from symbiote.agent_protocols import (
    AgentProtocol,
    AgentProtocolError,
    LegacyTextProtocol,
    PoolsideLagunaProtocol,
    create_default_protocol_registry,
)
from symbiote.agent_protocols.base import (
    PreparedTurn,
    ProtocolCapabilities,
    ProtocolState,
)
from symbiote.core.context import AssembledContext
from symbiote.environment.descriptors import (
    LLMResponse,
    NativeToolCall,
    ToolCall,
    ToolCallResult,
)


def _context(
    *,
    tool_loading: str = "full",
    tool_mode: str = "brief",
    generation_settings: dict | None = None,
) -> AssembledContext:
    return AssembledContext(
        symbiote_id="sym-1",
        session_id="session-1",
        user_input="run",
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


class ExplicitProtocol(AgentProtocol):
    protocol_id = "explicit_test_v1"
    capabilities = ProtocolCapabilities()

    def prepare_turn(
        self,
        context,
        *,
        host_overrides,
        native_tools,
        state,
    ) -> PreparedTurn:
        return PreparedTurn(
            protocol_id=self.protocol_id,
            native_tools=False,
            config={},
            tools=None,
            state=state,
        )

    def validate_response(self, response, calls, state) -> None:
        del response, calls, state

    def serialize_exchange(
        self,
        response,
        text,
        calls,
        results,
        *,
        tool_result_max_chars,
        state,
    ):
        return []


class TestProtocolRegistry:
    def test_default_falls_back_to_legacy(self) -> None:
        registry = create_default_protocol_registry()

        resolved = registry.resolve()

        assert isinstance(resolved, LegacyTextProtocol)
        assert resolved.protocol_id == "legacy_text_v1"

    def test_exact_poolside_override(self) -> None:
        registry = create_default_protocol_registry()

        resolved = registry.resolve("poolside_laguna_v1")

        assert isinstance(resolved, PoolsideLagunaProtocol)

    def test_explicit_instance_override(self) -> None:
        registry = create_default_protocol_registry()
        explicit = ExplicitProtocol()

        assert registry.resolve(explicit) is explicit

    def test_unknown_id_does_not_use_model_name_guessing(self) -> None:
        registry = create_default_protocol_registry()

        with pytest.raises(AgentProtocolError, match="unknown agent protocol"):
            registry.resolve("Laguna-S-2.1")

    def test_only_none_uses_fallback(self) -> None:
        registry = create_default_protocol_registry()

        with pytest.raises(AgentProtocolError, match="unknown agent protocol"):
            registry.resolve("")


class TestLegacyProtocol:
    def test_preparation_is_backward_compatible(self) -> None:
        protocol = LegacyTextProtocol()
        state = protocol.create_state()

        prepared = protocol.prepare_turn(
            _context(generation_settings={"temperature": 0.7}),
            host_overrides={"max_tokens": 111},
            native_tools=False,
            state=state,
        )

        assert prepared.native_tools is False
        assert prepared.tools is None
        assert prepared.config == {"temperature": 0.7, "max_tokens": 111}
        assert prepared.to_llm_kwargs() == {
            "config": {"temperature": 0.7, "max_tokens": 111}
        }

    def test_text_exchange_is_identical_to_legacy_format(self) -> None:
        protocol = LegacyTextProtocol()
        messages = protocol.serialize_exchange(
            'calling',
            "",
            [ToolCall(tool_id="lookup", params={"q": "x"})],
            [
                ToolCallResult(
                    tool_id="lookup",
                    success=True,
                    output={"value": 1},
                )
            ],
            tool_result_max_chars=2000,
            state=protocol.create_state(),
        )

        assert messages == [
            {
                "role": "assistant",
                "content": (
                    "```tool_call\n"
                    '{"tool": "lookup", "params": {"q": "x"}}\n'
                    "```"
                ),
            },
            {
                "role": "user",
                "content": '[Tool result: lookup]\n{"value": 1}',
            },
        ]

    def test_empty_text_tool_result_has_non_empty_marker(self) -> None:
        protocol = LegacyTextProtocol()

        messages = protocol.serialize_exchange(
            "calling",
            "",
            [ToolCall(tool_id="lookup")],
            [ToolCallResult(tool_id="lookup", success=True, output="   ")],
            tool_result_max_chars=2000,
            state=protocol.create_state(),
        )

        assert messages[1]["content"] == '[Tool result: lookup]\n"(no output)"'

    def test_exchange_cardinality_mismatch_is_not_silently_zipped(
        self,
    ) -> None:
        protocol = LegacyTextProtocol()

        with pytest.raises(AgentProtocolError, match="cardinality mismatch"):
            protocol.serialize_exchange(
                "calling",
                "",
                [ToolCall(tool_id="lookup")],
                [],
                tool_result_max_chars=2000,
                state=protocol.create_state(),
            )


class TestPoolsideLagunaProtocol:
    def test_index_mode_fails_before_preparation(self) -> None:
        protocol = PoolsideLagunaProtocol()

        with pytest.raises(AgentProtocolError, match="complete tool schemas"):
            protocol.prepare_turn(
                _context(tool_loading="index"),
                host_overrides=None,
                native_tools=True,
                state=protocol.create_state(),
            )

    @pytest.mark.parametrize("loading", ["full", "semantic", "semantic_full"])
    def test_only_full_schema_strategies_are_prepared(
        self,
        loading: str,
    ) -> None:
        protocol = PoolsideLagunaProtocol()
        state = protocol.create_state()

        prepared = protocol.prepare_turn(
            _context(tool_loading=loading),
            host_overrides=None,
            native_tools=False,
            state=state,
        )

        assert prepared.native_tools is True
        assert prepared.tools is not None
        parameters = prepared.tools[0]["function"]["parameters"]
        assert parameters == {"type": "object", "properties": {}}
        expected = "semantic_full" if loading.startswith("semantic") else "full"
        assert state.metadata["schema_strategy"] == expected

    def test_simple_profile_defaults_and_host_overrides_win(self) -> None:
        protocol = PoolsideLagunaProtocol()

        prepared = protocol.prepare_turn(
            _context(
                tool_mode="instant",
                generation_settings={"temperature": 0.4},
            ),
            host_overrides={
                "max_tokens": 333,
                "enable_thinking": True,
                "parallel_tool_calls": True,
            },
            native_tools=False,
            state=protocol.create_state(),
        )

        assert prepared.config["temperature"] == 0.4
        assert prepared.config["max_tokens"] == 333
        assert prepared.config["extra"]["chat_template_kwargs"] == {
            "enable_thinking": True
        }
        assert prepared.config["parallel_tool_calls"] is False
        assert "parallel_tool_calls" not in prepared.config["extra"]

    def test_extra_is_fail_closed_to_vendor_thinking_only(self) -> None:
        protocol = PoolsideLagunaProtocol()

        with pytest.raises(AgentProtocolError, match="unsupported keys"):
            protocol.prepare_turn(
                _context(),
                host_overrides={"extra": {"trace_id": "unsafe"}},
                native_tools=True,
                state=protocol.create_state(),
            )

        with pytest.raises(AgentProtocolError, match="unsupported keys"):
            protocol.prepare_turn(
                _context(),
                host_overrides={
                    "extra": {
                        "chat_template_kwargs": {
                            "enable_thinking": True,
                            "unknown_vendor_flag": True,
                        }
                    }
                },
                native_tools=True,
                state=protocol.create_state(),
            )

    @pytest.mark.parametrize(
        "source",
        [
            "top_level",
            "top_level_template",
            "extra_template",
        ],
    )
    @pytest.mark.parametrize("value", ["false", 0, 1, None])
    def test_enable_thinking_rejects_non_bool_values(
        self,
        source: str,
        value: object,
    ) -> None:
        protocol = PoolsideLagunaProtocol()
        if source == "top_level":
            overrides = {"enable_thinking": value}
        elif source == "top_level_template":
            overrides = {
                "chat_template_kwargs": {"enable_thinking": value}
            }
        else:
            overrides = {
                "extra": {
                    "chat_template_kwargs": {
                        "enable_thinking": value,
                    }
                }
            }

        with pytest.raises(AgentProtocolError, match="must be a bool"):
            protocol.prepare_turn(
                _context(),
                host_overrides=overrides,
                native_tools=True,
                state=protocol.create_state(),
            )

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"enable_thinking": False}, False),
            (
                {
                    "chat_template_kwargs": {
                        "enable_thinking": True,
                    }
                },
                True,
            ),
            (
                {
                    "extra": {
                        "chat_template_kwargs": {
                            "enable_thinking": False,
                        }
                    }
                },
                False,
            ),
        ],
    )
    def test_enable_thinking_accepts_real_bool_values(
        self,
        overrides: dict,
        expected: bool,
    ) -> None:
        protocol = PoolsideLagunaProtocol()

        prepared = protocol.prepare_turn(
            _context(),
            host_overrides=overrides,
            native_tools=True,
            state=protocol.create_state(),
        )

        assert prepared.config["extra"]["chat_template_kwargs"] == {
            "enable_thinking": expected
        }

    def test_complex_profile_is_selected_by_explicit_mode(self) -> None:
        protocol = PoolsideLagunaProtocol()
        state = protocol.create_state()

        prepared = protocol.prepare_turn(
            _context(tool_mode="brief"),
            host_overrides=None,
            native_tools=True,
            state=state,
        )

        assert state.profile == "complex"
        assert prepared.config["temperature"] == 0
        assert prepared.config["max_tokens"] == 768
        assert prepared.config["extra"]["chat_template_kwargs"] == {
            "enable_thinking": True
        }
        assert prepared.config["parallel_tool_calls"] is False

    def test_explicit_profile_does_not_inspect_user_text(self) -> None:
        protocol = PoolsideLagunaProtocol()
        context = _context(tool_mode="brief")
        context.user_input = "trivial hello"
        state = protocol.create_state()

        prepared = protocol.prepare_turn(
            context,
            host_overrides={"agent_profile": "simple"},
            native_tools=True,
            state=state,
        )

        assert state.profile == "simple"
        assert prepared.config["max_tokens"] == 256

    def test_reasoning_and_ids_are_replayed_in_native_exchange(self) -> None:
        protocol = PoolsideLagunaProtocol()
        response = LLMResponse(
            reasoning_content="private",
            reasoning_state={"signature": "opaque"},
            tool_calls=[
                NativeToolCall(
                    call_id="call-1",
                    tool_id="lookup",
                    params={"q": "x"},
                )
            ],
        )

        messages = protocol.serialize_exchange(
            response,
            "",
            [ToolCall(call_id="call-1", tool_id="lookup", params={"q": "x"})],
            [
                ToolCallResult(
                    tool_id="lookup",
                    success=True,
                    output={"value": 1},
                )
            ],
            tool_result_max_chars=2000,
            state=protocol.create_state(),
        )

        assert messages[0]["role"] == "assistant"
        assert messages[0]["reasoning_content"] == "private"
        assert messages[0]["reasoning_state"] == {"signature": "opaque"}
        assert messages[0]["tool_calls"][0]["id"] == "call-1"
        assert messages[1]["role"] == "tool"
        assert messages[1]["tool_call_id"] == "call-1"

    @pytest.mark.parametrize("call_id", [None, "", "   "])
    def test_missing_native_call_id_is_rejected(
        self,
        call_id: str | None,
    ) -> None:
        protocol = PoolsideLagunaProtocol()
        response = LLMResponse(
            tool_calls=[
                NativeToolCall(
                    call_id=call_id,
                    tool_id="lookup",
                )
            ]
        )

        with pytest.raises(
            AgentProtocolError,
            match="non-empty native call_id",
        ):
            protocol.validate_response(
                response,
                [ToolCall(call_id=call_id, tool_id="lookup")],
                protocol.create_state(),
            )

    def test_duplicate_native_call_ids_are_rejected(self) -> None:
        protocol = PoolsideLagunaProtocol()
        response = LLMResponse(
            tool_calls=[
                NativeToolCall(call_id="same", tool_id="lookup"),
                NativeToolCall(call_id="same", tool_id="lookup"),
            ]
        )

        with pytest.raises(
            AgentProtocolError,
            match="unique native call_id",
        ):
            protocol.validate_response(
                response,
                [
                    ToolCall(call_id="same", tool_id="lookup"),
                    ToolCall(call_id="same", tool_id="lookup"),
                ],
                protocol.create_state(),
            )

    def test_valid_native_call_ids_pass_validation(self) -> None:
        protocol = PoolsideLagunaProtocol()
        response = LLMResponse(
            tool_calls=[
                NativeToolCall(call_id="call-1", tool_id="lookup"),
                NativeToolCall(call_id="call-2", tool_id="lookup"),
            ]
        )

        protocol.validate_response(
            response,
            [
                ToolCall(call_id="call-1", tool_id="lookup"),
                ToolCall(call_id="call-2", tool_id="lookup"),
            ],
            protocol.create_state(),
        )

    def test_native_result_call_id_mismatch_is_rejected(self) -> None:
        protocol = PoolsideLagunaProtocol()

        with pytest.raises(
            AgentProtocolError,
            match="result correlation mismatch",
        ):
            protocol.serialize_exchange(
                LLMResponse(),
                "",
                [ToolCall(call_id="call-1", tool_id="lookup")],
                [
                    ToolCallResult(
                        call_id="call-other",
                        tool_id="lookup",
                        success=True,
                    )
                ],
                tool_result_max_chars=2000,
                state=protocol.create_state(),
            )

    def test_text_parsed_tool_call_is_rejected(self) -> None:
        protocol = PoolsideLagunaProtocol()

        with pytest.raises(AgentProtocolError, match="native structured"):
            protocol.validate_response(
                "```tool_call```",
                [ToolCall(tool_id="lookup")],
                protocol.create_state(),
            )
