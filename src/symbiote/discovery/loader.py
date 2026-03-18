"""DiscoveredToolLoader — activates approved discovered tools into the ToolGateway."""

from __future__ import annotations

from typing import TYPE_CHECKING

from symbiote.discovery.repository import DiscoveredToolRepository
from symbiote.environment.descriptors import HttpToolConfig, ToolDescriptor

if TYPE_CHECKING:
    from symbiote.environment.tools import ToolGateway


class DiscoveredToolLoader:
    """Bridges approved discovered_tools → ToolGateway.

    Usage (e.g. in YouNews lifespan after kernel init)::

        loader = DiscoveredToolLoader(
            DiscoveredToolRepository(kernel._storage),
            kernel.tool_gateway,
        )
        loaded = loader.load(clark_id, base_url="http://127.0.0.1:8000")
        kernel.environment.configure(symbiote_id=clark_id, tools=loaded)
    """

    def __init__(
        self,
        repository: DiscoveredToolRepository,
        gateway: ToolGateway,
    ) -> None:
        self._repo = repository
        self._gateway = gateway

    def load(self, symbiote_id: str, base_url: str = "") -> list[str]:
        """Register all approved HTTP tools for *symbiote_id* into the ToolGateway.

        Returns the list of tool_ids that were successfully registered so the
        caller can pass them to ``EnvironmentManager.configure()``.

        CLI tools (``handler_type=custom``) and tools without a method or
        url_template are skipped — they cannot be executed via HTTP.
        """
        approved = self._repo.list(symbiote_id, status="approved")
        registered: list[str] = []
        base = base_url.rstrip("/")

        for tool in approved:
            if tool.handler_type == "custom":
                continue
            if not tool.method or not tool.url_template:
                continue

            url = tool.url_template.replace("{base_url}", base)

            descriptor = ToolDescriptor(
                tool_id=tool.tool_id,
                name=tool.name,
                description=tool.description or tool.name,
                parameters=tool.parameters or {},
                handler_type="http",
            )
            config = HttpToolConfig(
                method=tool.method,  # type: ignore[arg-type]
                url_template=url,
                allow_internal=True,
            )
            self._gateway.register_http_tool(descriptor, config)
            registered.append(tool.tool_id)

        return registered
