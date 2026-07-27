"""Poolside Laguna native agent protocol."""

from __future__ import annotations

from typing import Any

from symbiote.agent_protocols.base import (
    AgentProtocol,
    AgentProtocolError,
    PreparedTurn,
    ProtocolCapabilities,
    ProtocolState,
    build_native_tool_schemas,
    merge_host_config,
    serialize_native_exchange,
)
from symbiote.core.context import AssembledContext
from symbiote.environment.descriptors import (
    LLMResponse,
    ToolCall,
    ToolCallResult,
)

_PROFILE_DEFAULTS: dict[str, dict[str, Any]] = {
    "simple": {
        "temperature": 0,
        "max_tokens": 256,
        "enable_thinking": False,
    },
    "complex": {
        "temperature": 0,
        "max_tokens": 768,
        "enable_thinking": True,
    },
}
_ALLOWED_EXTRA_KEYS = frozenset({
    "chat_template_kwargs",
    "parallel_tool_calls",
})
_ALLOWED_TEMPLATE_KEYS = frozenset({"enable_thinking"})
_UNSET = object()


class PoolsideLagunaProtocol(AgentProtocol):
    """Native protocol used by Poolside Laguna model families."""

    protocol_id = "poolside_laguna_v1"
    capabilities = ProtocolCapabilities(
        native_tools_required=True,
        reasoning_replay=True,
        structured_tool_history=True,
        allowed_schema_strategies=frozenset({"full", "semantic_full"}),
        parallel_tool_calls=False,
    )

    def prepare_turn(
        self,
        context: AssembledContext,
        *,
        host_overrides: dict[str, Any] | None,
        native_tools: bool,
        state: ProtocolState,
    ) -> PreparedTurn:
        schema_strategy = self._schema_strategy(context.tool_loading)
        config = self._prepare_config(context, host_overrides, state)
        state.metadata["schema_strategy"] = schema_strategy
        state.metadata["native_tools_forced"] = not native_tools

        return PreparedTurn(
            protocol_id=self.protocol_id,
            native_tools=True,
            config=config,
            tools=build_native_tool_schemas(context.available_tools),
            state=state,
        )

    def validate_response(
        self,
        response: str | LLMResponse,
        calls: list[ToolCall],
        state: ProtocolState,
    ) -> None:
        if calls and not isinstance(response, LLMResponse):
            raise AgentProtocolError(
                "poolside_laguna_v1 requires native structured tool calls; "
                "received a text-parsed tool call"
            )
        if not calls:
            return

        if len(response.tool_calls) != len(calls):
            raise AgentProtocolError(
                "poolside_laguna_v1 native call correlation mismatch"
            )

        call_ids: list[str] = []
        for native_call, call in zip(
            response.tool_calls,
            calls,
            strict=True,
        ):
            if (
                not isinstance(native_call.call_id, str)
                or not native_call.call_id.strip()
                or not isinstance(call.call_id, str)
                or not call.call_id.strip()
            ):
                raise AgentProtocolError(
                    "poolside_laguna_v1 requires non-empty native call_id"
                )
            if native_call.call_id != call.call_id:
                raise AgentProtocolError(
                    "poolside_laguna_v1 native call correlation mismatch"
                )
            call_ids.append(call.call_id)

        if len(call_ids) != len(set(call_ids)):
            raise AgentProtocolError(
                "poolside_laguna_v1 requires unique native call_id values"
            )

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
        if not isinstance(response, LLMResponse):
            raise AgentProtocolError(
                "poolside_laguna_v1 cannot serialize a textual tool exchange"
            )
        exchange = serialize_native_exchange(
            response,
            text,
            calls,
            results,
            max_chars=tool_result_max_chars,
        )
        state.exchange_count += 1
        return exchange

    def _schema_strategy(self, tool_loading: str) -> str:
        strategy = (
            "semantic_full"
            if tool_loading in {"semantic", "semantic_full"}
            else tool_loading
        )
        if strategy not in self.capabilities.allowed_schema_strategies:
            raise AgentProtocolError(
                "poolside_laguna_v1 requires complete tool schemas "
                "(tool_loading='full' or semantic full-schema selection); "
                f"tool_loading={tool_loading!r} is incompatible"
            )
        return strategy

    @staticmethod
    def _prepare_config(
        context: AssembledContext,
        host_overrides: dict[str, Any] | None,
        state: ProtocolState,
    ) -> dict[str, Any]:
        host_config = merge_host_config(context, host_overrides)
        profile = host_config.pop("agent_profile", None)
        if profile is None:
            profile = "simple" if context.tool_mode == "instant" else "complex"
        if profile not in _PROFILE_DEFAULTS:
            raise AgentProtocolError(
                "poolside_laguna_v1 agent_profile must be 'simple' or "
                f"'complex', got {profile!r}"
            )
        state.profile = profile

        defaults = _PROFILE_DEFAULTS[profile]
        config: dict[str, Any] = {
            "temperature": defaults["temperature"],
            "max_tokens": defaults["max_tokens"],
        }

        host_extra = host_config.pop("extra", {})
        if host_extra is None:
            host_extra = {}
        if not isinstance(host_extra, dict):
            raise AgentProtocolError(
                "poolside_laguna_v1 config 'extra' must be an object"
            )
        unsupported_extra = sorted(
            set(host_extra) - _ALLOWED_EXTRA_KEYS
        )
        if unsupported_extra:
            raise AgentProtocolError(
                "poolside_laguna_v1 config 'extra' contains unsupported "
                f"keys {unsupported_extra}; allowed={sorted(_ALLOWED_EXTRA_KEYS)}"
            )

        top_level_thinking = host_config.pop("enable_thinking", _UNSET)
        top_level_template = host_config.pop("chat_template_kwargs", None)
        host_config.pop("parallel_tool_calls", None)

        config.update(host_config)

        extra_template = host_extra.get("chat_template_kwargs")
        for source, template in (
            ("extra.chat_template_kwargs", extra_template),
            ("chat_template_kwargs", top_level_template),
        ):
            if template is not None and not isinstance(template, dict):
                raise AgentProtocolError(
                    f"poolside_laguna_v1 config {source!r} must be an object"
                )
            if isinstance(template, dict):
                unsupported_template = sorted(
                    set(template) - _ALLOWED_TEMPLATE_KEYS
                )
                if unsupported_template:
                    raise AgentProtocolError(
                        "poolside_laguna_v1 config "
                        f"{source!r} contains unsupported keys "
                        f"{unsupported_template}; "
                        f"allowed={sorted(_ALLOWED_TEMPLATE_KEYS)}"
                    )
                if (
                    "enable_thinking" in template
                    and not isinstance(template["enable_thinking"], bool)
                ):
                    raise AgentProtocolError(
                        "poolside_laguna_v1 config "
                        f"{source!r}.enable_thinking must be a bool"
                    )
        if (
            top_level_thinking is not _UNSET
            and not isinstance(top_level_thinking, bool)
        ):
            raise AgentProtocolError(
                "poolside_laguna_v1 config 'enable_thinking' must be a bool"
            )

        chat_template_kwargs: dict[str, Any] = {
            "enable_thinking": defaults["enable_thinking"]
        }
        if (
            isinstance(extra_template, dict)
            and "enable_thinking" in extra_template
        ):
            chat_template_kwargs["enable_thinking"] = (
                extra_template["enable_thinking"]
            )
        if (
            isinstance(top_level_template, dict)
            and "enable_thinking" in top_level_template
        ):
            chat_template_kwargs["enable_thinking"] = (
                top_level_template["enable_thinking"]
            )
        if top_level_thinking is not _UNSET:
            chat_template_kwargs["enable_thinking"] = top_level_thinking

        config["extra"] = {
            "chat_template_kwargs": chat_template_kwargs,
        }
        config["parallel_tool_calls"] = False
        return config
