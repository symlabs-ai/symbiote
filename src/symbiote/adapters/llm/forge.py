"""ForgeLLM adapter — delegates to forge_llm ChatAgent."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from symbiote.core.exceptions import LLMError, is_transient_llm_error, llm_error_status
from symbiote.core.llm_usage import report_usage, usage_from_provider
from symbiote.environment.descriptors import (
    LLMModelIdentity,
    LLMResponse,
    LLMTokenUsage,
    NativeToolCall,
    ReasoningDelta,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

try:
    from forge_llm import ChatAgent, ChatConfig, ChatMessage
except ImportError:  # pragma: no cover
    ChatAgent = None  # type: ignore[assignment, misc]
    ChatConfig = None  # type: ignore[assignment, misc]
    ChatMessage = None  # type: ignore[assignment, misc]


# ``project_slug`` not given: the slug this adapter has always sent to the gateway.
_DEFAULT_PROJECT_SLUG = "symbiote"
_UNSET: Any = object()


class ForgeLLMAdapter:
    """LLMPort implementation backed by forge_llm."""

    # Off unless asked; a subclass or a copy that skips __init__ sees it too.
    _stream_reasoning = False

    def __init__(
        self,
        provider: str = "anthropic",
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        stream_tool_calls: bool = False,
        project_slug: str | None = _UNSET,
        stream_reasoning: bool = False,
        provider_options: Mapping[str, Any] | None = None,
    ) -> None:
        """``stream_tool_calls``: stream text token by token also when native
        tools are offered, collecting the tool calls from the stream. Off by
        default: not every provider assembles tool calls in a stream (Groq).
        The reply then carries the reasoning the stream brought
        (``reasoning_content``, and the ``reasoning_state`` forge_llm 0.7.19
        streams for Anthropic's thinking); a provider whose stream brings no
        state cannot serve protocols that replay it (e.g.
        ``poolside_laguna_v1``).

        ``project_slug``: only for ``provider="symgateway"``, the project the
        gateway routes the call to (its ``X-Project-Slug`` header). Not given,
        it is ``"symbiote"``, as it always was; ``None`` sends no project.
        Every other provider is called with no gateway field at all.

        ``stream_reasoning``: ``stream()`` also yields the model's reasoning
        as ``ReasoningDelta`` items, never mixed with the text: the pieces a
        forge_llm stream chunk carries in ``reasoning_content`` (or
        ``reasoning``), and, when ``stream()`` falls back to ``complete()``
        (native tools without ``stream_tool_calls``), the reply's whole
        ``reasoning_content`` once, before its text. It changes neither the
        request nor the reply. Off by default: the stream yields exactly
        what it did.

        ``provider_options``: options for the provider, in its own terms,
        sent with every call as forge_llm's ``ChatConfig.extra`` (for example
        Anthropic's ``{"thinking": {"type": "enabled", "budget_tokens":
        8000}}`` or OpenAI's ``{"reasoning": {"summary": "auto"}}``). The
        ``provider_options`` of a call's own config is merged over them, key
        by key, as in ``SymGatewayLLMAdapter``, so one ``llm_config`` serves
        both adapters. The attribute may be replaced after construction.
        """
        self._stream_tool_calls = stream_tool_calls
        self._stream_reasoning = stream_reasoning
        self.provider_options: dict[str, Any] = dict(provider_options or {})
        # Read by the kernel's usage record when a response names neither.
        self.provider = provider
        self.model = model
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
            # The gateway's project is a gateway matter: other providers
            # get nothing that speaks of it.
            slug = _DEFAULT_PROJECT_SLUG if project_slug is _UNSET else project_slug
            if provider == "symgateway" and slug:
                kwargs["extra"] = {"project_slug": slug}
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
            fields = self._config_fields(config)
            if fields or tools:
                cfg = ChatConfig(**fields)
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
            # Plain text carries no usage: hand it to the kernel directly.
            _report(_extract_usage(response), _extract_model_identity(response))
            return response.content or ""
        except LLMError:
            raise
        except Exception as exc:
            raise _wrap_error(exc) from exc

    def stream(
        self,
        messages: list[dict],
        config: dict | None = None,
        tools: list[dict] | None = None,
    ) -> Iterator[str | ReasoningDelta | LLMResponse]:
        """Stream tokens from the LLM.

        Yields ``str`` chunks for text tokens. If the model emits native
        tool calls, yields a final ``LLMResponse`` after all text chunks.
        With ``stream_reasoning``, ``ReasoningDelta`` items for the
        reasoning, as it arrives.

        When tools are provided and the provider doesn't support true
        streaming with tool calls (e.g. Groq), falls back to ``complete()``
        and yields the result as a single item.
        """
        if tools and self._stream_tool_calls:
            yield from self._stream_with_tools(messages, config, tools)
            return
        # When native tools are requested, fall back to complete() because
        # most providers don't stream tool call responses properly.
        if tools:
            result = self.complete(messages, config=config, tools=tools)
            if isinstance(result, LLMResponse):
                if self._stream_reasoning and result.reasoning_content:
                    yield ReasoningDelta(text=result.reasoning_content)
                if result.content:
                    yield result.content
                yield result
            else:
                yield result
            return

        try:
            chat_messages = [_to_chat_message(m) for m in messages]
            kwargs: dict = {"messages": chat_messages}
            fields = self._config_fields(config)
            if fields or _STREAM_USAGE:
                kwargs["config"] = _stream_config(fields)

            usage: Any = None
            for chunk in self._agent.stream_chat(**kwargs):
                usage = _value(chunk, "usage") or usage
                thought = self._reasoning(chunk)
                if thought is not None:
                    yield thought
                if hasattr(chunk, "content") and chunk.content:
                    yield chunk.content
                elif isinstance(chunk, str):
                    yield chunk
            _report(_extract_usage({"usage": usage}), None)
        except LLMError:
            raise
        except Exception as exc:
            raise _wrap_error(exc) from exc

    def _stream_with_tools(
        self,
        messages: list[dict],
        config: dict | None,
        tools: list[dict],
    ) -> Iterator[str | ReasoningDelta | LLMResponse]:
        """Text chunks as they arrive, then an ``LLMResponse`` if the model
        called tools (the provider assembles the calls at the end)."""
        try:
            cfg = _stream_config(self._config_fields(config))
            cfg.tools = tools
            parts: list[str] = []
            thoughts: list[str] = []
            reasoning_state: Any = None
            raw_calls: list | None = None
            finish_reason: str | None = None
            usage: Any = None
            for chunk in self._agent.stream_chat(
                messages=[_to_chat_message(m) for m in messages],
                config=cfg,
                auto_execute_tools=False,
            ):
                piece = _reasoning_text(chunk)
                if piece:
                    thoughts.append(piece)
                    if self._stream_reasoning:
                        yield ReasoningDelta(text=piece)
                reasoning_state = _value(chunk, "reasoning_state") or reasoning_state
                text = _value(chunk, "content")
                if isinstance(text, str) and text:
                    parts.append(text)
                    yield text
                raw_calls = _value(chunk, "tool_calls") or raw_calls
                finish_reason = _value(chunk, "finish_reason") or finish_reason
                usage = _value(chunk, "usage") or usage
        except LLMError:
            raise
        except Exception as exc:
            raise _wrap_error(exc) from exc
        if raw_calls:
            yield LLMResponse(
                content="".join(parts),
                tool_calls=_extract_tool_calls({"tool_calls": raw_calls}),
                # What the provider may need back with the tool results.
                reasoning_content="".join(thoughts) or None,
                reasoning_state=reasoning_state,
                finish_reason=finish_reason,
                usage=_extract_usage({"usage": usage}),
            )
        else:
            _report(_extract_usage({"usage": usage}), None)

    def _reasoning(self, chunk: Any) -> ReasoningDelta | None:
        """The reasoning a stream chunk carries, when it is to be streamed."""
        if not self._stream_reasoning:
            return None
        text = _reasoning_text(chunk)
        return ReasoningDelta(text=text) if text else None

    def _config_fields(self, config: dict | None) -> dict[str, Any]:
        """The ChatConfig fields of a call: its config, with the provider
        options (the adapter's, then the call's) in ``extra``."""
        fields = dict(config or {})
        own = fields.pop("provider_options", None)
        options = {
            **(getattr(self, "provider_options", None) or {}),
            **(own if isinstance(own, Mapping) else {}),
        }
        if options:
            extra = fields.get("extra")
            fields["extra"] = {**(extra if isinstance(extra, Mapping) else {}), **options}
        return fields


def _reasoning_text(chunk: Any) -> str | None:
    """The piece of reasoning a forge_llm stream chunk carries, if any."""
    if isinstance(chunk, str):
        return None
    for name in ("reasoning_content", "reasoning"):
        text = _value(chunk, name)
        if isinstance(text, str) and text:
            return text
    return None


def _wrap_error(exc: Exception) -> LLMError:
    """LLMError keeping the provider status and whether a retry may help."""
    return LLMError(
        str(exc),
        status_code=llm_error_status(exc),
        retryable=is_transient_llm_error(exc),
    )


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


def _report(usage: LLMTokenUsage | None, identity: LLMModelIdentity | None) -> None:
    """Tell the kernel what a call that returns plain text cost."""
    if usage is None and identity is None:
        return
    report_usage(
        usage=usage,
        provider=identity.provider if identity else None,
        model=identity.model if identity else None,
    )


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
    """What the call cost, in the kernel's convention (``prompt_tokens`` is
    the whole input, cache included).

    forge_llm hands over the provider's own counters in ``raw`` (0.7.17 and
    later): they are read from there, prompt cache included, and each
    provider's way of counting is turned into the one convention. An older
    forge_llm gives three counters and nothing about the cache.
    """
    usage = _value(response, "token_usage", _value(response, "usage"))
    raw = _value(usage, "raw")
    return usage_from_provider(raw if isinstance(raw, dict) and raw else usage)


# A stream reports its usage only when asked (forge_llm 0.7.17 and later).
_STREAM_USAGE = "include_usage" in getattr(ChatConfig, "__dataclass_fields__", {})


def _stream_config(fields: dict | None) -> Any:
    """The ChatConfig of a streamed call, asking for its token usage."""
    cfg = ChatConfig(**(fields or {}))
    if _STREAM_USAGE:
        cfg.include_usage = True
    return cfg


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
