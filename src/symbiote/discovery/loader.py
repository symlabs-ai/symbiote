"""DiscoveredToolLoader — activates approved discovered tools into the ToolGateway."""

from __future__ import annotations

from collections.abc import Callable
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
        loaded = loader.load(
            clark_id,
            base_url="http://127.0.0.1:8000",
            header_factory=lambda: {"Cookie": f"access_token=Bearer {token}"},
        )
        kernel.environment.configure(symbiote_id=clark_id, tools=loaded)
    """

    def __init__(
        self,
        repository: DiscoveredToolRepository,
        gateway: ToolGateway,
    ) -> None:
        self._repo = repository
        self._gateway = gateway

    def load(
        self,
        symbiote_id: str,
        base_url: str = "",
        header_factory: Callable[[], dict[str, str]] | None = None,
    ) -> list[str]:
        """Register all approved HTTP tools for *symbiote_id* into the ToolGateway.

        Args:
            symbiote_id: The symbiote to load tools for.
            base_url: Base URL prepended to relative url_templates.
            header_factory: Optional callable invoked per-request to supply
                dynamic headers (e.g. auth cookies). Applied to all loaded tools.

        Returns the list of tool_ids that were successfully registered so the
        caller can pass them to ``EnvironmentManager.configure()``.

        CLI tools (``handler_type=custom``) and tools without a method or
        url_template are skipped — they cannot be executed via HTTP.

        For POST/PUT/PATCH tools, a ``body_template`` is auto-derived from the
        tool's parameter schema (properties become ``{placeholder}`` values).
        Query-only GET tools get ``optional_params`` derived from non-required
        parameters.
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
                tags=tool.tags,
                handler_type="http",
            )

            body_template, optional_params, array_params = _derive_http_config(
                tool.method, url, tool.parameters or {}
            )

            config = HttpToolConfig(
                method=tool.method,  # type: ignore[arg-type]
                url_template=url,
                allow_internal=True,
                header_factory=header_factory,
                body_template=body_template,
                optional_params=optional_params,
                array_params=array_params,
            )
            self._gateway.register_http_tool(descriptor, config)
            registered.append(tool.tool_id)

        return registered


def _derive_http_config(
    method: str, url_template: str, parameters: dict,
) -> tuple[dict | None, list[str], list[str]]:
    """Derive body_template, optional_params, and array_params from OpenAPI schema.

    For POST/PUT/PATCH:
        - Path params (present in url_template as {name}) are excluded from body.
        - Remaining params become body_template: {"key": "{key}"}.
        - Array-typed params are added to array_params.

    For GET/DELETE:
        - Non-required params listed in the URL are marked optional_params.
        - No body_template.
    """
    props = parameters.get("properties", {})
    required = set(parameters.get("required", []))

    if not props:
        return None, [], []

    # Identify path params (before '?') vs query params (after '?')
    import re
    path_part = url_template.split("?", 1)[0]
    path_params = set(re.findall(r"\{(\w+)\}", path_part))

    # Identify array params
    array_params = [
        name for name, schema in props.items()
        if schema.get("type") == "array"
    ]

    if method in ("POST", "PUT", "PATCH"):
        # Build body_template from non-path params
        body = {}
        for name in props:
            if name not in path_params:
                body[name] = f"{{{name}}}"
        return (body or None), [], array_params

    # GET/DELETE — optional_params are non-required query params
    optional = [
        name for name in props
        if name not in required and name not in path_params
    ]
    return None, optional, []
