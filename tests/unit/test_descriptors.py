"""Tests for ToolDescriptor, HttpToolConfig, ToolCall, ToolCallResult models."""

from __future__ import annotations

from symbiote.environment.descriptors import (
    HttpToolConfig,
    LLMResponse,
    NativeToolCall,
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

    def test_tags_default_empty(self) -> None:
        d = ToolDescriptor(tool_id="t", name="T", description="T")
        assert d.tags == []

    def test_tags_stored(self) -> None:
        d = ToolDescriptor(
            tool_id="search",
            name="Search",
            description="Search items",
            tags=["Items", "Compose"],
        )
        assert d.tags == ["Items", "Compose"]


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


# ── Native function calling models ──────────────────────────────────────────


class TestNativeToolCall:
    def test_create_minimal(self) -> None:
        tc = NativeToolCall(tool_id="search", params={"q": "test"})
        assert tc.tool_id == "search"
        assert tc.call_id is None
        assert tc.params == {"q": "test"}

    def test_create_with_call_id(self) -> None:
        tc = NativeToolCall(
            call_id="call_abc123",
            tool_id="publish",
            params={"id": "42"},
        )
        assert tc.call_id == "call_abc123"
        assert tc.tool_id == "publish"

    def test_to_tool_call(self) -> None:
        tc = NativeToolCall(
            call_id="call_xyz",
            tool_id="search",
            params={"q": "news"},
        )
        converted = tc.to_tool_call()
        assert isinstance(converted, ToolCall)
        assert converted.tool_id == "search"
        assert converted.params == {"q": "news"}


class TestLLMResponse:
    def test_create_text_only(self) -> None:
        r = LLMResponse(content="Hello!")
        assert r.content == "Hello!"
        assert r.tool_calls == []

    def test_create_with_tool_calls(self) -> None:
        r = LLMResponse(
            content="I'll search for that.",
            tool_calls=[
                NativeToolCall(call_id="c1", tool_id="search", params={"q": "test"}),
            ],
        )
        assert r.content == "I'll search for that."
        assert len(r.tool_calls) == 1
        assert r.tool_calls[0].tool_id == "search"

    def test_empty_content_default(self) -> None:
        r = LLMResponse(
            tool_calls=[NativeToolCall(tool_id="list", params={})]
        )
        assert r.content == ""
        assert len(r.tool_calls) == 1


class TestToolDescriptorOpenAISchema:
    def test_to_openai_schema(self) -> None:
        d = ToolDescriptor(
            tool_id="search",
            name="Search",
            description="Search items",
            parameters={
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        )
        schema = d.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "search"
        assert schema["function"]["description"] == "Search items"
        assert schema["function"]["parameters"]["required"] == ["q"]

    def test_to_openai_schema_empty_params(self) -> None:
        d = ToolDescriptor(tool_id="list", name="List", description="List all")
        schema = d.to_openai_schema()
        assert schema["function"]["parameters"] == {"type": "object", "properties": {}}


class TestHttpToolConfigOptionalAndArrayParams:
    def test_optional_params_default_empty(self) -> None:
        c = HttpToolConfig(url_template="http://localhost/api/items")
        assert c.optional_params == []

    def test_array_params_default_empty(self) -> None:
        c = HttpToolConfig(url_template="http://localhost/api/items")
        assert c.array_params == []

    def test_optional_params_stored(self) -> None:
        c = HttpToolConfig(
            url_template="http://localhost/api/items?status={status}&limit={limit}",
            optional_params=["status", "limit"],
        )
        assert "status" in c.optional_params
        assert "limit" in c.optional_params

    def test_array_params_stored(self) -> None:
        c = HttpToolConfig(
            url_template="http://localhost/api/bulk",
            method="POST",
            body_template={"item_ids": "{item_ids}", "action": "{action}"},
            array_params=["item_ids"],
        )
        assert "item_ids" in c.array_params


class TestHttpToolConfigHeaderFactory:
    def test_header_factory_default_none(self) -> None:
        c = HttpToolConfig(url_template="http://localhost/api")
        assert c.header_factory is None

    def test_header_factory_accepts_callable(self) -> None:
        token_store = {"value": "tok-123"}
        c = HttpToolConfig(
            url_template="http://localhost/api",
            header_factory=lambda: {"Authorization": f"Bearer {token_store['value']}"},
        )
        assert c.header_factory is not None
        assert c.header_factory() == {"Authorization": "Bearer tok-123"}

    def test_header_factory_overrides_static_headers(self) -> None:
        """header_factory return value should take precedence over static headers."""
        c = HttpToolConfig(
            url_template="http://localhost/api",
            headers={"Authorization": "Bearer static"},
            header_factory=lambda: {"Authorization": "Bearer dynamic"},
        )
        merged = dict(c.headers)
        if c.header_factory:
            merged.update(c.header_factory())
        assert merged["Authorization"] == "Bearer dynamic"
