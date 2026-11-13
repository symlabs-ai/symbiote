"""ForgeLLM adapter — delegates to forge_llm ChatAgent."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

from symbiote.core.exceptions import LLMError
from symbiote.environment.descriptors import (
    LLMModelIdentity,
    LLMResponse,
    LLMTokenUsage,
    NativeToolCall,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

try:
    from forge_llm import ChatAgent, ChatConfig, ChatMessage
except ImportError:  # pragma: no cover
    ChatAgent = None  # type: ignore[assignment, misc]
    ChatConfig = None  # type: ignore[assignment, misc]
    ChatMessage = None  # type: ignore[assignment, misc]


class ForgeLLMAdapter:
    """LLMPort implementation backed by forge_llm."""

    def __init__(
        self,
        provider: str = "anthropic",
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        if ChatAgent is None:
            raise LLMError(
                "forge_llm is not installed — run `pip install forge-llm`"
            )

        try:
            prefix = provider.upper()
            # Resolve API key: explicit > env var ({PROVIDER}_API_KEY)
            if api_key is None:
                api_key = os.environ.get(f"{prefix}_API_KEY")
            # Resolve base URL: explicit > env var ({PROVIDER}_BASE_URL)
            if base_url is None:
                base_url = os.environ.get(f"{prefix}_BASE_URL")

            kwargs: dict = {"provider": provider, "max_retries": 0}
            if model is not None:
                kwargs["model"] = model
            if api_key is not None:
                kwargs["api_key"] = api_key
            if base_url is not None:
                kwargs["base_url"] = base_url
            # Inject project slug for symgateway routing
            kwargs["extra"] = {"project_slug": "symbiote"}
            self._agent = ChatAgent(**kwargs)
        except Exception as exc:
            raise LLMError(str(exc)) from exc

    def complete(
        self,
        messages: list[dict],
        config: dict | None = None,
        tools: list[dict] | None = None,
    ) -> str | LLMResponse:
        """Send messages to the LLM and return the response.

        Returns a plain ``str`` when the model responds with text only.
        Returns an ``LLMResponse`` when native tool calls are present,
        allowing the ChatRunner to handle them without text-based parsing.
        """
        try:
            chat_messages = [_to_chat_message(m) for m in messages]
            kwargs: dict = {"messages": chat_messages}
            if config or tools:
                cfg = ChatConfig(**(config or {}))
                if tools:
                    cfg.tools = tools
                kwargs["config"] = cfg
            response = self._agent.chat(**kwargs)

            # Check for native tool calls
            tc_list = _extract_tool_calls(response)
            reasoning_content, reasoning_state = _extract_reasoning(response)
            if tc_list or reasoning_content is not None or reasoning_state is not None:
                return LLMResponse(
                    content=response.content or "",
                    tool_calls=tc_list,
                    reasoning_content=reasoning_content,
                    reasoning_state=reasoning_state,
                    finish_reason=_extract_finish_reason(response),
                    model_identity=_extract_model_identity(response),
                    usage=_extract_usage(response),
                    metadata=_extract_opaque_metadata(response),
                )
            return response.content or ""
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(str(exc)) from exc

    def stream(
        self,
        messages: list[dict],
        config: dict | None = None,
        tools: list[dict] | None = None,
    ) -> Iterator[str | LLMResponse]:
        """Stream tokens from the LLM.

        Yields ``str`` chunks for text tokens. If the model emits native
        tool calls, yields a final ``LLMResponse`` after all text chunks.

        When tools are provided and the provider doesn't support true
        streaming with tool calls (e.g. Groq), falls back to ``complete()``
        and yields the result as a single item.
        """
        # When native tools are requested, fall back to complete() because
        # most providers don't stream tool call responses properly.
        if tools:
            result = self.complete(messages, config=config, tools=tools)
            if isinstance(result, LLMResponse):
                if result.content:
                    yield result.content
                yield result
            else:
                yield result
            return

        try:
            chat_messages = [_to_chat_message(m) for m in messages]
            kwargs: dict = {"messages": chat_messages}
            if config:
                cfg = ChatConfig(**(config or {}))
                kwargs["config"] = cfg

            for chunk in self._agent.stream_chat(**kwargs):
                if hasattr(chunk, "content") and chunk.content:
                    yield chunk.content
                elif isinstance(chunk, str):
                    yield chunk
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(str(exc)) from exc


def _to_chat_message(message: dict[str, Any]) -> Any:
    """Convert a canonical wire dict without flattening native fields."""
    return ChatMessage(
        role=message["role"],
        content=message.get("content"),
        name=message.get("name"),
        tool_calls=message.get("tool_calls"),
        tool_call_id=message.get("tool_call_id"),
        reasoning_content=message.get("reasoning_content"),
        reasoning_state=message.get("reasoning_state"),
    )


def _value(source: Any, key: str, default: Any = None) -> Any:
    if source is None:
        return default
    if isinstance(source, dict):
        return source.get(key, default)
    values = getattr(source, "__dict__", {})
    if isinstance(values, dict) and key in values:
        return values[key]
    if hasattr(type(source), key):
        return getattr(source, key)
    return default


def _raw_response(response: Any) -> Any:
    metadata = _value(response, "metadata")
    return _value(metadata, "raw_response")


def _extract_reasoning(response: Any) -> tuple[str | None, Any]:
    """Extract ForgeLLM's first-class ephemeral reasoning fields."""
    reasoning_content = _value(response, "reasoning_content")
    reasoning_state = _value(response, "reasoning_state")
    message = _value(response, "message")
    if not isinstance(reasoning_content, str):
        reasoning_content = _value(message, "reasoning_content")
    if not isinstance(reasoning_content, str):
        reasoning_content = None
    if reasoning_state is None:
        reasoning_state = _value(message, "reasoning_state")
    return reasoning_content, reasoning_state


def _extract_finish_reason(response: Any) -> str | None:
    metadata = _value(response, "metadata")
    finish_reason = _value(metadata, "finish_reason")
    if finish_reason is None:
        finish_reason = _value(response, "finish_reason")
    return str(finish_reason) if finish_reason is not None else None


def _extract_model_identity(response: Any) -> LLMModelIdentity | None:
    metadata = _value(response, "metadata")
    model = _value(metadata, "model", _value(response, "model"))
    provider = _value(metadata, "provider", _value(response, "provider"))
    if model is None and provider is None:
        return None
    return LLMModelIdentity(
        provider=str(provider) if provider is not None else None,
        model=str(model) if model is not None else None,
    )


def _extract_usage(response: Any) -> LLMTokenUsage | None:
    usage = _value(response, "token_usage", _value(response, "usage"))
    if usage is None:
        return None

    prompt_tokens = _value(
        usage,
        "prompt_tokens",
        _value(usage, "input_tokens"),
    )
    completion_tokens = _value(
        usage,
        "completion_tokens",
        _value(usage, "output_tokens"),
    )
    total_tokens = _value(usage, "total_tokens")
    if total_tokens is None and (
        prompt_tokens is not None or completion_tokens is not None
    ):
        total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)

    known = {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
    }
    if isinstance(usage, dict):
        extra = {key: value for key, value in usage.items() if key not in known}
    else:
        extra = {
            key: value
            for key, value in vars(usage).items()
            if key not in known
        } if hasattr(usage, "__dict__") else {}

    return LLMTokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        metadata=extra,
    )


def _extract_opaque_metadata(response: Any) -> dict[str, Any]:
    metadata = _value(response, "metadata")
    excluded = {
        "model",
        "provider",
        "finish_reason",
        "raw_response",
        "reasoning_content",
        "reasoning_state",
    }
    if isinstance(metadata, dict):
        opaque = {
            key: value
            for key, value in metadata.items()
            if key not in excluded
        }
    elif hasattr(metadata, "__dict__"):
        opaque = {
            key: value
            for key, value in vars(metadata).items()
            if key not in excluded
        }
    else:
        opaque = {}

    raw = _raw_response(response)
    for key in ("id", "request_id", "system_fingerprint"):
        value = _value(raw, key, _value(response, key))
        if value is not None:
            opaque[key] = value
    return opaque


def _decode_arguments(raw: Any) -> tuple[dict, str | None]:
    """Decode native tool-call arguments into a dict.

    Returns ``({}, error)`` when they are not a JSON object — typically
    truncated by ``finish_reason=length``. Running the tool with ``{}``
    instead would silently drop every argument (``delete_records`` with no
    filter), so the caller reports the error to the model instead.
    """
    if raw is None or raw == "":
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    if not isinstance(raw, str):
        return {}, f"Tool arguments must be a JSON object, got {type(raw).__name__}"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"Tool arguments are not valid JSON (possibly truncated): {exc.msg}"
    if not isinstance(value, dict):
        return {}, f"Tool arguments must be a JSON object, got {type(value).__name__}"
    return value, None


def _extract_tool_calls(response: Any) -> list[NativeToolCall]:
    """Extract NativeToolCall list from a forge_llm response."""
    tool_calls: list[NativeToolCall] = []
    # ChatResponse exposes ``tool_calls`` directly. The message fallback keeps
    # compatibility with older ForgeLLM response objects.
    raw_calls = _value(response, "tool_calls")
    if raw_calls is None:
        raw_calls = _value(_value(response, "message"), "tool_calls")

    if not raw_calls:
        return []

    for tc in raw_calls:
        if isinstance(tc, dict):
            # OpenAI format: {"id": "...", "type": "function", "function": {"name": "...", "arguments": "..."}}
            func = tc.get("function", {})
            name = func.get("name", "")
            params, error = _decode_arguments(func.get("arguments", "{}"))
            tool_calls.append(NativeToolCall(
                call_id=tc.get("id"),
                tool_id=name,
                params=params,
                arguments_error=error,
            ))
        elif hasattr(tc, "function"):
            # Object format
            name = tc.function.name if hasattr(tc.function, "name") else ""
            args_str = tc.function.arguments if hasattr(tc.function, "arguments") else "{}"
            params, error = _decode_arguments(args_str)
            tool_calls.append(NativeToolCall(
                call_id=getattr(tc, "id", None),
                tool_id=name,
                params=params,
                arguments_error=error,
            ))

    return tool_calls
