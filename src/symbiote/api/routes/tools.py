"""HTTP tools registered through the API."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.api.auth import APIKey
from symbiote.api.http import (
    _require_symbiote_access,
    get_adapter,
    get_env_manager,
    get_kernel,
    get_tool_gateway,
)
from symbiote.api.middleware import (
    require_auth,
)
from symbiote.api.schemas import (
    RegisterToolRequest,
    ToolDescriptorResponse,
    ToolExecRequest,
    ToolExecResponse,
)
from symbiote.core.kernel import SymbioteKernel
from symbiote.environment.descriptors import HttpToolConfig, ToolDescriptor
from symbiote.environment.manager import EnvironmentManager
from symbiote.environment.tools import ToolGateway

router = APIRouter()


# ── Tool endpoints ───────────────────────────────────────────────────────


@router.post(
    "/symbiotes/{symbiote_id}/tools",
    status_code=201,
    response_model=ToolDescriptorResponse,
)
def register_tool(
    symbiote_id: str,
    body: RegisterToolRequest,
    auth: Annotated[APIKey, Depends(require_auth)],
    adapter: Annotated[SQLiteAdapter, Depends(get_adapter)],
    env: Annotated[EnvironmentManager, Depends(get_env_manager)],
    kernel: Annotated[SymbioteKernel, Depends(get_kernel)],
) -> ToolDescriptorResponse:
    """Register an HTTP tool for a symbiote (persisted; 409 on a name clash)."""
    _require_symbiote_access(adapter, symbiote_id, auth)
    descriptor = ToolDescriptor(
        tool_id=body.tool_id,
        name=body.name,
        description=body.description,
        parameters=body.parameters,
        handler_type="http",
    )
    http_config = HttpToolConfig(
        method=body.http_method,
        url_template=body.url_template,
        headers=body.headers,
        timeout=body.timeout,
        body_template=body.body_template,
    )
    kernel.register_http_tool(symbiote_id, descriptor, http_config)

    return ToolDescriptorResponse(
        tool_id=descriptor.tool_id,
        name=descriptor.name,
        description=descriptor.description,
        parameters=descriptor.parameters,
        handler_type=descriptor.handler_type,
    )


@router.get(
    "/symbiotes/{symbiote_id}/tools",
    response_model=list[ToolDescriptorResponse],
)
def list_tools(
    symbiote_id: str,
    auth: Annotated[APIKey, Depends(require_auth)],
    adapter: Annotated[SQLiteAdapter, Depends(get_adapter)],
    env: Annotated[EnvironmentManager, Depends(get_env_manager)],
    gw: Annotated[ToolGateway, Depends(get_tool_gateway)],
) -> list[ToolDescriptorResponse]:
    """List tools available to a symbiote (authorized ones only)."""
    _require_symbiote_access(adapter, symbiote_id, auth)
    authorized = set(env.list_tools(symbiote_id))
    return [
        ToolDescriptorResponse(
            tool_id=d.tool_id,
            name=d.name,
            description=d.description,
            parameters=d.parameters,
            handler_type=d.handler_type,
        )
        for d in gw.get_descriptors()
        if d.tool_id in authorized
    ]


@router.delete("/symbiotes/{symbiote_id}/tools/{tool_id}", status_code=200)
def remove_tool(
    symbiote_id: str,
    tool_id: str,
    auth: Annotated[APIKey, Depends(require_auth)],
    adapter: Annotated[SQLiteAdapter, Depends(get_adapter)],
    kernel: Annotated[SymbioteKernel, Depends(get_kernel)],
) -> dict:
    """Remove a tool from a symbiote.

    Builtins and tools of other symbiotes are only de-authorized for this
    symbiote, never unregistered for everyone.
    """
    _require_symbiote_access(adapter, symbiote_id, auth)
    deleted = kernel.remove_http_tool(symbiote_id, tool_id)
    return {"removed": tool_id, "deleted": deleted}


@router.post(
    "/symbiotes/{symbiote_id}/tools/{tool_id}/exec",
    response_model=ToolExecResponse,
)
def exec_tool(
    symbiote_id: str,
    tool_id: str,
    body: ToolExecRequest,
    auth: Annotated[APIKey, Depends(require_auth)],
    adapter: Annotated[SQLiteAdapter, Depends(get_adapter)],
    gw: Annotated[ToolGateway, Depends(get_tool_gateway)],
) -> ToolExecResponse:
    """Execute a tool manually (for testing)."""
    _require_symbiote_access(adapter, symbiote_id, auth)
    result = gw.execute(
        symbiote_id=symbiote_id,
        session_id=None,
        tool_id=tool_id,
        params=body.params,
    )
    return ToolExecResponse(
        tool_id=tool_id,
        success=result.success,
        output=result.output,
        error=result.error,
    )
