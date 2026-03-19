"""Tool descriptors, HTTP config, and tool call models."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ToolDescriptor(BaseModel):
    """Schema describing a tool so the LLM knows how to call it."""

    tool_id: str
    name: str
    description: str
    parameters: dict = Field(default_factory=dict)  # JSON Schema
    tags: list[str] = Field(default_factory=list)
    handler_type: Literal["builtin", "http", "custom"] = "custom"

    def to_openai_schema(self) -> dict:
        """Convert to OpenAI function calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.tool_id,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }


class HttpToolConfig(BaseModel):
    """Declarative HTTP tool definition."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url_template: str  # e.g. "http://127.0.0.1:8000/api/items/{id}/publish"
    headers: dict[str, str] = Field(default_factory=dict)
    header_factory: Callable[[], dict[str, str]] | None = None
    """Optional callable invoked per-request to supply dynamic headers (e.g. auth tokens).
    Its return value is merged on top of ``headers`` at call time, so it can
    override static headers as well as add new ones."""
    allow_internal: bool = False
    """When True, skip SSRF validation so the tool can call loopback / private-network
    endpoints (e.g. a service running on the same host).  Only set this for tools
    that intentionally target internal services you control."""
    timeout: float = 30.0
    body_template: dict | None = None  # JSON body template with {param} placeholders
    optional_params: list[str] = Field(default_factory=list)
    """Params listed here are removed from the URL template when absent or empty,
    instead of raising KeyError.  Any ``{param}`` placeholder — along with its
    surrounding query-string segment (``&key={param}`` or ``?key={param}``) — is
    stripped from the URL before the request is sent."""
    array_params: list[str] = Field(default_factory=list)
    """Params listed here are serialised as JSON arrays in the request body rather
    than being coerced to a plain string via ``str.format``.  Only relevant when
    ``body_template`` is set."""


class ToolCall(BaseModel):
    """A tool call parsed from LLM response text."""

    tool_id: str
    params: dict[str, Any] = Field(default_factory=dict)


class ToolCallResult(BaseModel):
    """Result of executing a parsed tool call."""

    tool_id: str
    success: bool
    output: Any = None
    error: str | None = None


# ── Native function calling models ──────────────────────────────────────────


class NativeToolCall(BaseModel):
    """A tool call returned by the LLM via native function calling.

    Maps to ToolCall for execution but carries the provider's call_id
    so the host can correlate results back to the provider if needed.
    """

    call_id: str | None = None  # provider-assigned ID (e.g. OpenAI tool_call.id)
    tool_id: str
    params: dict[str, Any] = Field(default_factory=dict)

    def to_tool_call(self) -> ToolCall:
        """Convert to a ToolCall for gateway execution."""
        return ToolCall(tool_id=self.tool_id, params=self.params)


class LLMResponse(BaseModel):
    """Structured response from an LLM that may contain native tool calls.

    When an LLM adapter supports native function calling, it returns this
    instead of a plain str.  ChatRunner detects the type and uses native
    tool_calls directly, skipping text-based parsing.
    """

    content: str = ""
    tool_calls: list[NativeToolCall] = Field(default_factory=list)
