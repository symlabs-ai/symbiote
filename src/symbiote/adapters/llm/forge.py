"""ForgeLLM adapter — delegates to forge_llm ChatAgent."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from symbiote.core.exceptions import LLMError
from symbiote.environment.descriptors import LLMResponse, NativeToolCall

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
            chat_messages = [
                ChatMessage(role=m["role"], content=m["content"]) for m in messages
            ]
            kwargs: dict = {"messages": chat_messages}
            if config or tools:
                cfg = ChatConfig(**(config or {}))
                if tools:
                    cfg.tools = tools
                kwargs["config"] = cfg
            response = self._agent.chat(**kwargs)

            # Check for native tool calls
            tc_list = _extract_tool_calls(response)
            if tc_list:
                return LLMResponse(
                    content=response.content or "",
                    tool_calls=tc_list,
                )
            return response.content
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
            chat_messages = [
                ChatMessage(role=m["role"], content=m["content"]) for m in messages
            ]
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


def _extract_tool_calls(response) -> list[NativeToolCall]:
    """Extract NativeToolCall list from a forge_llm response."""
    tool_calls = []
    raw_calls = None

    # Try response.message.tool_calls (forge_llm standard)
    if hasattr(response, "message") and hasattr(response.message, "tool_calls"):
        raw_calls = response.message.tool_calls
    # Fallback: response.tool_calls
    elif hasattr(response, "tool_calls"):
        raw_calls = response.tool_calls

    if not raw_calls:
        return []

    for tc in raw_calls:
        if isinstance(tc, dict):
            # OpenAI format: {"id": "...", "type": "function", "function": {"name": "...", "arguments": "..."}}
            func = tc.get("function", {})
            name = func.get("name", "")
            args_str = func.get("arguments", "{}")
            try:
                params = json.loads(args_str) if isinstance(args_str, str) else args_str
            except (json.JSONDecodeError, TypeError):
                params = {}
            tool_calls.append(NativeToolCall(
                call_id=tc.get("id"),
                tool_id=name,
                params=params,
            ))
        elif hasattr(tc, "function"):
            # Object format
            name = tc.function.name if hasattr(tc.function, "name") else ""
            args_str = tc.function.arguments if hasattr(tc.function, "arguments") else "{}"
            try:
                params = json.loads(args_str) if isinstance(args_str, str) else args_str
            except (json.JSONDecodeError, TypeError):
                params = {}
            tool_calls.append(NativeToolCall(
                call_id=getattr(tc, "id", None),
                tool_id=name,
                params=params,
            ))

    return tool_calls
