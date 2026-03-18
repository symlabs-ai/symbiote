"""Tests for McpToolProvider — bridges forge_llm ToolRegistry → ToolGateway."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from symbiote.mcp.provider import McpToolProvider

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_forge_tool(name: str, description: str = "A tool", parameters: dict | None = None):
    """Create a mock forge_llm IToolPort with a ToolDefinition."""
    defn = MagicMock()
    defn.name = name
    defn.description = description
    defn.parameters = parameters or {}

    tool = MagicMock()
    tool.definition = defn
    tool.execute_async = AsyncMock()
    return tool


def _make_registry(*tools):
    """Create a mock ToolRegistry containing *tools*."""
    registry = MagicMock()
    registry.list_tools.return_value = [t.definition.name for t in tools]
    registry.get.side_effect = lambda name: next(
        (t for t in tools if t.definition.name == name), None
    )
    return registry


def _make_gateway():
    """Return a MagicMock ToolGateway that records register_descriptor calls."""
    gw = MagicMock()
    gw.register_descriptor = MagicMock()
    return gw


# ── McpToolProvider.load() ────────────────────────────────────────────────────


class TestLoad:
    def test_registers_all_tools(self):
        t1 = _make_forge_tool("get_weather")
        t2 = _make_forge_tool("search_docs")
        registry = _make_registry(t1, t2)
        gateway = _make_gateway()

        provider = McpToolProvider()
        result = provider.load(registry, gateway)

        assert result == ["get_weather", "search_docs"]
        assert gateway.register_descriptor.call_count == 2

    def test_returns_empty_for_empty_registry(self):
        registry = _make_registry()
        gateway = _make_gateway()

        result = McpToolProvider().load(registry, gateway)

        assert result == []
        gateway.register_descriptor.assert_not_called()

    def test_descriptor_fields_mapped_correctly(self):
        tool = _make_forge_tool(
            "my_tool",
            description="Does things",
            parameters={"type": "object", "properties": {"x": {"type": "string"}}},
        )
        registry = _make_registry(tool)
        gateway = _make_gateway()

        McpToolProvider().load(registry, gateway)

        descriptor, _ = gateway.register_descriptor.call_args[0]
        assert descriptor.tool_id == "my_tool"
        assert descriptor.name == "my_tool"
        assert descriptor.description == "Does things"
        assert descriptor.parameters == {"type": "object", "properties": {"x": {"type": "string"}}}

    def test_hyphen_in_name_converted_to_underscore(self):
        tool = _make_forge_tool("get-weather")
        registry = _make_registry(tool)
        gateway = _make_gateway()

        result = McpToolProvider().load(registry, gateway)

        assert result == ["get_weather"]
        descriptor, _ = gateway.register_descriptor.call_args[0]
        assert descriptor.tool_id == "get_weather"

    def test_spaces_in_name_converted_to_underscore(self):
        tool = _make_forge_tool("get weather")
        registry = _make_registry(tool)
        gateway = _make_gateway()

        result = McpToolProvider().load(registry, gateway)

        assert result == ["get_weather"]

    def test_empty_description_uses_name_as_fallback(self):
        tool = _make_forge_tool("my_tool", description="")
        registry = _make_registry(tool)
        gateway = _make_gateway()

        McpToolProvider().load(registry, gateway)

        descriptor, _ = gateway.register_descriptor.call_args[0]
        assert descriptor.description == "my_tool"

    def test_none_parameters_defaults_to_empty_dict(self):
        tool = _make_forge_tool("my_tool")
        tool.definition.parameters = None
        registry = _make_registry(tool)
        gateway = _make_gateway()

        McpToolProvider().load(registry, gateway)

        descriptor, _ = gateway.register_descriptor.call_args[0]
        assert descriptor.parameters == {}

    def test_skips_tool_when_registry_get_returns_none(self):
        registry = MagicMock()
        registry.list_tools.return_value = ["ghost"]
        registry.get.return_value = None
        gateway = _make_gateway()

        result = McpToolProvider().load(registry, gateway)

        assert result == []
        gateway.register_descriptor.assert_not_called()


# ── Handler execution ─────────────────────────────────────────────────────────


class TestHandler:
    @pytest.mark.asyncio
    async def test_handler_calls_execute_async(self):
        tool = _make_forge_tool("search")
        forge_result = MagicMock()
        forge_result.is_error = False
        forge_result.content = "found it"
        tool.execute_async.return_value = forge_result

        registry = _make_registry(tool)
        gateway = _make_gateway()

        with patch("forge_llm.domain.entities.ToolCall"):
            McpToolProvider().load(registry, gateway)

        _, handler = gateway.register_descriptor.call_args[0]
        result = await handler({"query": "hello"})
        assert result == "found it"
        tool.execute_async.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handler_raises_on_mcp_error(self):
        tool = _make_forge_tool("search")
        forge_result = MagicMock()
        forge_result.is_error = True
        forge_result.content = "tool error"
        tool.execute_async.return_value = forge_result

        registry = _make_registry(tool)
        gateway = _make_gateway()

        with patch("forge_llm.domain.entities.ToolCall"):
            McpToolProvider().load(registry, gateway)

        _, handler = gateway.register_descriptor.call_args[0]
        with pytest.raises(RuntimeError, match="tool error"):
            await handler({})

    @pytest.mark.asyncio
    async def test_each_handler_calls_its_own_tool(self):
        """Closure captures correct tool per iteration — no late-binding bug."""
        t1 = _make_forge_tool("tool_a")
        t2 = _make_forge_tool("tool_b")

        r1 = MagicMock(is_error=False, content="from_a")
        r2 = MagicMock(is_error=False, content="from_b")
        t1.execute_async.return_value = r1
        t2.execute_async.return_value = r2

        registry = _make_registry(t1, t2)
        gateway = _make_gateway()

        with patch("forge_llm.domain.entities.ToolCall"):
            McpToolProvider().load(registry, gateway)

        calls = gateway.register_descriptor.call_args_list
        _, handler_a = calls[0][0]
        _, handler_b = calls[1][0]

        out_a = await handler_a({})
        out_b = await handler_b({})

        assert out_a == "from_a"
        assert out_b == "from_b"
        t1.execute_async.assert_awaited_once()
        t2.execute_async.assert_awaited_once()


# ── Kernel integration ────────────────────────────────────────────────────────


class TestKernelIntegration:
    def test_kernel_load_mcp_tools_registers_and_authorizes(self, tmp_path):
        from symbiote.config.models import KernelConfig
        from symbiote.core.kernel import SymbioteKernel

        cfg = KernelConfig(db_path=str(tmp_path / "test.db"))
        kernel = SymbioteKernel(cfg)
        sym = kernel.create_symbiote("clark", "Clark")

        tool = _make_forge_tool("mcp_search", description="Search MCP")
        registry = _make_registry(tool)

        with patch("symbiote.mcp.provider.McpToolProvider.load", return_value=["mcp_search"]) as mock_load:
            tool_ids = kernel.load_mcp_tools(registry, symbiote_id=sym.id)

        assert tool_ids == ["mcp_search"]
        mock_load.assert_called_once()

    def test_kernel_load_mcp_tools_returns_empty_when_no_tools(self, tmp_path):
        from symbiote.config.models import KernelConfig
        from symbiote.core.kernel import SymbioteKernel

        cfg = KernelConfig(db_path=str(tmp_path / "test.db"))
        kernel = SymbioteKernel(cfg)
        sym = kernel.create_symbiote("clark", "Clark")

        registry = _make_registry()

        with patch("symbiote.mcp.provider.McpToolProvider.load", return_value=[]):
            tool_ids = kernel.load_mcp_tools(registry, symbiote_id=sym.id)

        assert tool_ids == []
