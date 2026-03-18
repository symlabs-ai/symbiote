"""McpToolProvider — bridges a forge_llm ToolRegistry into Symbiote's ToolGateway.

Usage (host application):

    from forge_llm.mcp import McpToolset
    from symbiote.mcp import McpToolProvider

    async with McpToolset.from_stdio("python", ["my_server.py"]) as registry:
        provider = McpToolProvider()
        tool_ids = provider.load(registry, kernel.tool_gateway)
        kernel.authorize_tools(symbiote_id, tool_ids)

    # Or use the kernel convenience method:
    async with McpToolset.from_http("http://localhost:8000/mcp") as registry:
        tool_ids = kernel.load_mcp_tools(registry, symbiote_id="clark")
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from symbiote.environment.descriptors import ToolDescriptor
from symbiote.environment.tools import ToolGateway

if TYPE_CHECKING:
    from forge_llm.application.tools import ToolRegistry


class McpToolProvider:
    """Registers all tools from a forge_llm ToolRegistry into Symbiote's ToolGateway.

    Each MCP tool is bridged as an async custom handler: the handler receives
    ``params: dict`` from the gateway, constructs a forge_llm ToolCall, and
    awaits ``McpTool.execute_async()``.  Errors are surfaced as RuntimeError so
    PolicyGate captures them as failed tool results.

    The forge_llm ToolRegistry (and the underlying MCP session) must remain alive
    for as long as the registered handlers are callable.  Manage the McpToolset
    context manager lifetime in the host application accordingly.
    """

    def load(self, registry: ToolRegistry, gateway: ToolGateway) -> list[str]:
        """Register all MCP tools from *registry* into *gateway*.

        Args:
            registry: Live forge_llm ToolRegistry produced by McpToolset.
            gateway: Symbiote ToolGateway to register tools into.

        Returns:
            List of registered tool_ids (sanitized MCP tool names).
        """
        from forge_llm.domain.entities import ToolCall as ForgeToolCall

        registered: list[str] = []

        for tool_name in registry.list_tools():
            tool = registry.get(tool_name)
            if tool is None:
                continue

            defn = tool.definition
            tool_id = defn.name.replace("-", "_").replace(" ", "_")

            descriptor = ToolDescriptor(
                tool_id=tool_id,
                name=defn.name,
                description=defn.description or defn.name,
                parameters=defn.parameters or {},
                handler_type="custom",
            )

            # Capture per-iteration references for the closure
            _tool = tool
            _name = defn.name

            async def _handler(
                params: dict[str, Any],
                _t: Any = _tool,
                _n: str = _name,
            ) -> str:
                forge_call = ForgeToolCall(id="", name=_n, arguments=params)
                result = await _t.execute_async(forge_call)
                if result.is_error:
                    raise RuntimeError(result.content)
                return result.content

            gateway.register_descriptor(descriptor, _handler)
            registered.append(tool_id)

        return registered
