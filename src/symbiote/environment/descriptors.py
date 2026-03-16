"""Tool descriptors, HTTP config, and tool call models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolDescriptor(BaseModel):
    """Schema describing a tool so the LLM knows how to call it."""

    tool_id: str
    name: str
    description: str
    parameters: dict = Field(default_factory=dict)  # JSON Schema
    handler_type: Literal["builtin", "http", "custom"] = "custom"


class HttpToolConfig(BaseModel):
    """Declarative HTTP tool definition."""

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url_template: str  # e.g. "http://127.0.0.1:8000/api/items/{id}/publish"
    headers: dict[str, str] = Field(default_factory=dict)
    timeout: float = 30.0
    body_template: dict | None = None  # JSON body template with {param} placeholders


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
