"""Versioned contracts for model-facing agent protocols."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from symbiote.environment.descriptors import (
    LLMResponse,
    ToolCall,
    ToolCallResult,
    ToolDescriptor,
)

if TYPE_CHECKING:
    from symbiote.core.context import AssembledContext


logger = logging.getLogger(__name__)

# Tools whose cut result was already reported at WARNING (later cuts: DEBUG).
_cut_reported: set[str] = set()
_EMPTY_TOOL_RESULT = "(no output)"


class AgentProtocolError(ValueError):
    """Raised when a protocol cannot safely prepare or replay a turn."""


@dataclass(frozen=True)
class ProtocolCapabilities:
    """Static behavior advertised by an agent protocol implementation."""

    native_tools_required: bool = False
    reasoning_replay: bool = False
    structured_tool_history: bool = False
    allowed_schema_strategies: frozenset[str] = frozenset(
        {"full", "index", "semantic_full"}
    )
    parallel_tool_calls: bool | None = None


@dataclass
class ProtocolState:
    """Ephemeral state for one runner invocation.

    Reasoning content is deliberately not stored here. It remains only in the
    in-memory wire messages produced by ``serialize_exchange``.
    """

    protocol_id: str
    profile: str | None = None
    exchange_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PreparedTurn:
    """Protocol-prepared LLM request settings for a complete tool loop."""

    protocol_id: str
    native_tools: bool
    config: dict[str, Any]
    tools: list[dict[str, Any]] | None
    state: ProtocolState

    def to_llm_kwargs(self) -> dict[str, Any]:
        """Return kwargs accepted by ``LLMPort.complete/stream``."""
        kwargs: dict[str, Any] = {"config": dict(self.config)}
        if self.tools is not None:
            kwargs["tools"] = list(self.tools)
        return kwargs


class AgentProtocol(ABC):
    """Interface implemented by every versioned agent wire protocol."""

    protocol_id: str
    capabilities: ProtocolCapabilities

    def create_state(self) -> ProtocolState:
        """Create isolated, in-memory state for one runner invocation."""
        return ProtocolState(protocol_id=self.protocol_id)

    @abstractmethod
    def prepare_turn(
        self,
        context: AssembledContext,
        *,
        host_overrides: dict[str, Any] | None,
        native_tools: bool,
        state: ProtocolState,
    ) -> PreparedTurn:
        """Prepare generation settings and visible native tool schemas."""

    @abstractmethod
    def validate_response(
        self,
        response: str | LLMResponse,
        calls: list[ToolCall],
        state: ProtocolState,
    ) -> None:
        """Validate a response before any tool side effect occurs."""

    @abstractmethod
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
        """Serialize one assistant/tool exchange for the next LLM request."""


def merge_host_config(
    context: AssembledContext,
    host_overrides: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge host settings without mutating the assembled context."""
    config = dict(context.generation_settings or {})
    if host_overrides:
        config.update(host_overrides)
    return config


def build_native_tool_schemas(
    available_tools: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Build complete OpenAI function schemas, including no-argument tools."""
    if not available_tools:
        return None

    schemas: list[dict[str, Any]] = []
    for raw_tool in available_tools:
        schema = ToolDescriptor(**raw_tool).to_openai_schema()
        parameters = schema["function"].get("parameters")
        if not isinstance(parameters, dict) or not parameters:
            schema["function"]["parameters"] = {
                "type": "object",
                "properties": {},
            }
        else:
            parameters.setdefault("type", "object")
            parameters.setdefault("properties", {})
        schemas.append(schema)
    return schemas


def format_legacy_assistant(text: str, calls: list[ToolCall]) -> str:
    """Render the historical fenced ``tool_call`` assistant message."""
    parts: list[str] = []
    if text:
        parts.append(text)
    for call in calls:
        block = json.dumps(
            {"tool": call.tool_id, "params": call.params},
            ensure_ascii=False,
        )
        parts.append(f"```tool_call\n{block}\n```")
    return "\n".join(parts)


def _non_empty_tool_result(value: Any) -> Any:
    """Return a provider-safe representation for an empty tool result.

    JSON objects and lists are already non-empty text when serialized. Only a
    missing or blank scalar result needs a marker so no adapter can emit an
    empty tool-result text block.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return _EMPTY_TOOL_RESULT
    return value


def compact_tool_result(
    result_text: str,
    max_chars: int,
    *,
    tool_id: str | None = None,
) -> str:
    """Apply the runner's historical per-result truncation contract.

    A cut is logged with the tool, the sizes and the setting that decides it
    (never the content): the host still receives the whole result, so without
    the log nothing tells it that the model read less. The first cut of each
    tool is a WARNING, the following ones DEBUG.
    """
    if len(result_text) <= max_chars:
        return result_text
    truncated = result_text[:max_chars]
    remaining = len(result_text) - max_chars
    name = tool_id or "unknown"
    level = logging.DEBUG if name in _cut_reported else logging.WARNING
    _cut_reported.add(name)
    logger.log(
        level,
        "tool result cut before reaching the model: tool=%s, %d chars, limit %d "
        "(KernelConfig.tool_result_max_chars); the model does not see the last "
        "%d. Raise tool_result_max_chars together with loop_context_budget.",
        name,
        len(result_text),
        max_chars,
        remaining,
    )
    return f"{truncated}\n... ({remaining} chars truncated)"


def format_legacy_tool_results(
    results: list[ToolCallResult],
    *,
    max_chars: int,
) -> str:
    """Render the historical aggregated user tool-result message."""
    parts: list[str] = []
    for result in results:
        if result.success:
            output = _non_empty_tool_result(result.output)
            raw = (
                f"[Tool result: {result.tool_id}]\n"
                f"{json.dumps(output, default=str, ensure_ascii=False)}"
            )
        else:
            error = _non_empty_tool_result(result.error)
            raw = f"[Tool error: {result.tool_id}]\n{error}"
        parts.append(compact_tool_result(raw, max_chars, tool_id=result.tool_id))
    return "\n\n".join(parts)


def ensure_exchange_cardinality(
    calls: list[ToolCall],
    results: list[ToolCallResult],
) -> None:
    """Reject incomplete exchanges instead of silently truncating with zip."""
    if len(calls) != len(results):
        raise AgentProtocolError(
            "tool exchange cardinality mismatch: "
            f"{len(calls)} calls but {len(results)} results"
        )


def serialize_legacy_exchange(
    text: str,
    calls: list[ToolCall],
    results: list[ToolCallResult],
    *,
    max_chars: int,
) -> list[dict[str, Any]]:
    """Serialize the legacy assistant/user exchange exactly as before."""
    ensure_exchange_cardinality(calls, results)
    return [
        {
            "role": "assistant",
            "content": format_legacy_assistant(text, calls),
        },
        {
            "role": "user",
            "content": format_legacy_tool_results(
                results,
                max_chars=max_chars,
            ),
        },
    ]


def serialize_native_exchange(
    response: LLMResponse,
    text: str,
    calls: list[ToolCall],
    results: list[ToolCallResult],
    *,
    max_chars: int,
) -> list[dict[str, Any]]:
    """Serialize structured assistant/tool history with reasoning replay."""
    ensure_exchange_cardinality(calls, results)
    for call, result in zip(calls, results, strict=True):
        if (
            result.call_id is not None
            and result.call_id != call.call_id
        ):
            raise AgentProtocolError(
                "native tool result correlation mismatch: "
                f"call_id={call.call_id!r}, result.call_id={result.call_id!r}"
            )

    assistant_message: dict[str, Any] = {
        "role": "assistant",
        "content": text,
        "tool_calls": [
            {
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.tool_id,
                    "arguments": json.dumps(
                        call.params,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            }
            for call in calls
        ],
    }
    if response.reasoning_content is not None:
        assistant_message["reasoning_content"] = response.reasoning_content
    if response.reasoning_state is not None:
        assistant_message["reasoning_state"] = response.reasoning_state

    messages = [assistant_message]
    for call, result in zip(calls, results, strict=True):
        payload: dict[str, Any] = {"success": result.success}
        if result.success:
            payload["output"] = _non_empty_tool_result(result.output)
        else:
            payload["error"] = _non_empty_tool_result(result.error)
        content = json.dumps(
            payload,
            default=str,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": result.call_id or call.call_id,
                "name": call.tool_id,
                "content": compact_tool_result(
                    content, max_chars, tool_id=call.tool_id
                ),
            }
        )
    return messages


def flatten_tool_exchanges(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return *messages* with structured tool exchanges rewritten as text.

    For a model call made without ``tools``: several providers refuse a
    history that carries tool-call or tool-result blocks when the request
    declares no tool. An assistant message keeps its text followed by one
    ``[Called tool: name]`` line per call; the ``tool`` messages that answer
    it become one ``user`` message, as in the text protocol. Messages without
    tool structures are returned unchanged, and *messages* is not modified.
    """
    flat: list[dict[str, Any]] = []
    merging_results = False
    for message in messages:
        if message.get("role") == "tool":
            name = message.get("name") or message.get("tool_call_id") or "unknown"
            block = f"[Tool result: {name}]\n{_text_content(message)}"
            if merging_results:
                flat[-1]["content"] += f"\n\n{block}"
            else:
                flat.append({"role": "user", "content": block})
                merging_results = True
            continue
        merging_results = False
        calls = message.get("tool_calls")
        if not calls:
            flat.append(message)
            continue
        text = _text_content(message)
        parts = [text] if text else []
        for call in calls:
            function = call.get("function") or {}
            arguments = function.get("arguments") or "{}"
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, default=str, ensure_ascii=False)
            parts.append(
                f"[Called tool: {function.get('name', 'unknown')}]\n{arguments}"
            )
        rewritten = {k: v for k, v in message.items() if k != "tool_calls"}
        rewritten["content"] = "\n".join(parts)
        flat.append(rewritten)
    return flat


def _text_content(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "" if content is None else json.dumps(content, default=str, ensure_ascii=False)
