"""Backward-compatible Symbiote agent protocol."""

from __future__ import annotations

from typing import Any

from symbiote.agent_protocols.base import (
    AgentProtocol,
    PreparedTurn,
    ProtocolCapabilities,
    ProtocolState,
    build_native_tool_schemas,
    merge_host_config,
    serialize_legacy_exchange,
    serialize_native_exchange,
)
from symbiote.core.context import AssembledContext
from symbiote.environment.descriptors import (
    LLMResponse,
    ToolCall,
    ToolCallResult,
)


class LegacyTextProtocol(AgentProtocol):
    """Current Symbiote behavior, retained as the default protocol."""

    protocol_id = "legacy_text_v1"
    capabilities = ProtocolCapabilities()

    def prepare_turn(
        self,
        context: AssembledContext,
        *,
        host_overrides: dict[str, Any] | None,
        native_tools: bool,
        state: ProtocolState,
    ) -> PreparedTurn:
        tools = (
            build_native_tool_schemas(context.available_tools)
            if native_tools
            else None
        )
        return PreparedTurn(
            protocol_id=self.protocol_id,
            native_tools=native_tools,
            config=merge_host_config(context, host_overrides),
            tools=tools,
            state=state,
        )

    def validate_response(
        self,
        response: str | LLMResponse,
        calls: list[ToolCall],
        state: ProtocolState,
    ) -> None:
        """The legacy protocol accepts both text and structured responses."""
        del response, calls, state

    def serialize_exchange(
        self,
        response: str | LLMResponse,
        text: str,
        calls: list[ToolCall],
        results: list[ToolCallResult],
        *,
        tool_result_max_chars: int,
        state: ProtocolState,
    ) -> list[dict[str, Any]]:
        if isinstance(response, LLMResponse):
            exchange = serialize_native_exchange(
                response,
                text,
                calls,
                results,
                max_chars=tool_result_max_chars,
            )
        else:
            exchange = serialize_legacy_exchange(
                text,
                calls,
                results,
                max_chars=tool_result_max_chars,
            )
        state.exchange_count += 1
        return exchange
