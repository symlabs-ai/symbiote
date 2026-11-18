"""DiscoveredToolLoader — activates approved discovered tools into the ToolGateway."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from symbiote.discovery.repository import DiscoveredToolRepository
from symbiote.environment.descriptors import HttpToolConfig, ToolDescriptor

if TYPE_CHECKING:
    from symbiote.environment.tools import ToolGateway


logger = logging.getLogger(__name__)

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
        kernel.environment.add_tools(clark_id, loaded)
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
        base_origin = _origin(base) if base else None

        for tool in approved:
            if tool.handler_type == "custom":
                continue
            if not tool.method or not tool.url_template:
                continue
            existing = self._gateway.get_descriptor(tool.tool_id)
            if existing is not None and existing.handler_type != "http":
                # A spec operationId must never replace a builtin/custom tool
                # (e.g. "bash") for every symbiote on the gateway.
                logger.warning(
                    "Discovered tool %r collides with a %s tool; not loaded",
                    tool.tool_id, existing.handler_type,
                )
                continue

            url = tool.url_template.replace("{base_url}", base)
            # Host credentials and internal-network access only for the host's
            # own API (same origin as base_url). With no base_url the human
            # approval is the only gate, as before.
            trusted = base_origin is None or _origin(url) == base_origin

            descriptor = ToolDescriptor(
                tool_id=tool.tool_id,
                name=tool.name,
                description=tool.description or tool.name,
                parameters=tool.parameters or {},
                tags=tool.tags,
                handler_type="http",
                risk_level=tool.risk_level,
            )

            config = http_config_for(
                tool.method,
                url,
                tool.parameters or {},
                header_factory=header_factory if trusted else None,
                allow_internal=trusted,
            )
            self._gateway.register_http_tool(descriptor, config)
            registered.append(tool.tool_id)

        return registered


def _origin(url: str) -> tuple[str, str, int | None]:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    default = {"http": 80, "https": 443}.get(parts.scheme)
    return (parts.scheme, (parts.hostname or "").lower(), parts.port or default)


def http_config_for(
    method: str,
    url: str,
    parameters: dict | None = None,
    *,
    headers: dict[str, str] | None = None,
    header_factory: Callable[[], dict[str, str]] | None = None,
    allow_internal: bool = False,
    timeout: float = 30.0,
) -> HttpToolConfig:
    """The ``HttpToolConfig`` of an HTTP operation, from its method, its URL
    template and the JSON Schema of its parameters (as ``parse_openapi``
    gives them): which parameters go in the body, which are optional in the
    query, and which are lists.

    Builds only: nothing is registered or authorized. A host registers the
    result with ``ToolGateway.register_http_tool`` under the id it chooses,
    and the rules of encoding, origin and network are applied when the tool
    runs, by the gateway.

    ``headers`` and ``header_factory`` are the credential of this one tool:
    the factory is called on every request. ``allow_internal`` lets the tool
    reach loopback and private addresses; leave it ``False`` for an API that
    is not the host's own.
    """
    parameters = parameters or {}
    method = method.upper()
    body_template, optional_params, array_params = _derive_http_config(
        method, url, parameters
    )
    return HttpToolConfig(
        method=method,  # type: ignore[arg-type]
        url_template=url,
        headers=dict(headers or {}),
        header_factory=header_factory,
        allow_internal=allow_internal,
        timeout=timeout,
        body_template=body_template,
        optional_params=optional_params,
        array_params=array_params,
        # Set by the conversion when the body is not JSON (a form, a file).
        content_type=parameters.get("_content_type", "application/json"),
    )


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
    path_part, _, query_part = url_template.partition("?")
    path_params = set(re.findall(r"\{([^{}]+)\}", path_part))
    query_params = set(re.findall(r"\{([^{}]+)\}", query_part))

    # Identify array params
    array_params = [
        name for name, schema in props.items()
        if schema.get("type") == "array"
    ]

    if method in ("POST", "PUT", "PATCH"):
        # Build body_template from required non-path params only.
        # Optional params are omitted — they'll be included in the body
        # only if the LLM explicitly provides them.
        body = {}
        for name in props:
            if name not in path_params and name not in query_params and name in required:
                body[name] = f"{{{name}}}"
        optional_query = [name for name in query_params if name not in required]
        return (body or None), optional_query, array_params

    # GET/DELETE — optional_params are non-required query params
    optional = [
        name for name in props
        if name not in required and name not in path_params
    ]
    return None, optional, []
