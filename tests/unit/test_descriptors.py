"""Tests for ToolDescriptor, HttpToolConfig, ToolCall, ToolCallResult models."""

from __future__ import annotations

from symbiote.environment.descriptors import (
    HttpToolConfig,
    ToolCall,
    ToolCallResult,
    ToolDescriptor,
)


class TestToolDescriptor:
    def test_create_minimal(self) -> None:
        d = ToolDescriptor(tool_id="echo", name="Echo", description="Echoes input")
        assert d.tool_id == "echo"
        assert d.handler_type == "custom"
        assert d.parameters == {}

    def test_create_with_params(self) -> None:
        d = ToolDescriptor(
            tool_id="search",
            name="Search",
            description="Search items",
            parameters={
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
            handler_type="http",
        )
        assert d.parameters["required"] == ["q"]
        assert d.handler_type == "http"

    def test_handler_types(self) -> None:
        for ht in ("builtin", "http", "custom"):
            d = ToolDescriptor(tool_id="t", name="T", description="T", handler_type=ht)
            assert d.handler_type == ht


class TestHttpToolConfig:
    def test_defaults(self) -> None:
        c = HttpToolConfig(url_template="http://localhost/api")
        assert c.method == "GET"
        assert c.timeout == 30.0
        assert c.headers == {}
        assert c.body_template is None

    def test_full_config(self) -> None:
        c = HttpToolConfig(
            method="POST",
            url_template="http://localhost/api/items/{id}/publish",
            headers={"Authorization": "Bearer tok"},
            timeout=10.0,
            body_template={"title": "{title}"},
        )
        assert c.method == "POST"
        assert "{id}" in c.url_template
        assert c.body_template == {"title": "{title}"}


class TestToolCall:
    def test_create(self) -> None:
        tc = ToolCall(tool_id="publish", params={"id": "123"})
        assert tc.tool_id == "publish"
        assert tc.params == {"id": "123"}

    def test_empty_params(self) -> None:
        tc = ToolCall(tool_id="list")
        assert tc.params == {}


class TestToolCallResult:
    def test_success(self) -> None:
        r = ToolCallResult(tool_id="pub", success=True, output={"status": "ok"})
        assert r.success is True
        assert r.error is None

    def test_failure(self) -> None:
        r = ToolCallResult(tool_id="pub", success=False, error="not found")
        assert r.success is False
        assert r.output is None
