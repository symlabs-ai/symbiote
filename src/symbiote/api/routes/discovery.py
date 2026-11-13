"""Tool discovery, review and approval."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.api.auth import APIKey
from symbiote.api.http import (
    _require_symbiote,
    get_adapter,
    get_discovery_service,
    get_identity_manager,
)
from symbiote.api.middleware import (
    require_auth,
)
from symbiote.api.schemas import (
    ClassifyToolsRequest,
    ClassifyToolsResponse,
    DiscoveredToolResponse,
    DiscoverRequest,
    DiscoverResponse,
    ResetToolsResponse,
    UpdateDiscoveredToolRequest,
)
from symbiote.core.identity import IdentityManager
from symbiote.discovery.models import DiscoveredTool
from symbiote.discovery.repository import DiscoveredToolRepository
from symbiote.discovery.service import DiscoveryService

router = APIRouter()


# ── Discovery endpoints ────────────────────────────────────────────────────


@router.post("/symbiotes/{symbiote_id}/discover", response_model=DiscoverResponse, status_code=200)
def discover(
    symbiote_id: str,
    body: DiscoverRequest,
    auth: Annotated[APIKey, Depends(require_auth)],
    identity: Annotated[IdentityManager, Depends(get_identity_manager)],
    svc: Annotated[DiscoveryService, Depends(get_discovery_service)],
) -> DiscoverResponse:
    """Scan a local repository path and register discovered tools for a Symbiote."""
    _require_symbiote(identity, symbiote_id, auth)

    result = svc.discover(symbiote_id=symbiote_id, source_path=body.source_path, url=body.url)
    return DiscoverResponse(
        discovered=result.count,
        tools=[_tool_to_response(t) for t in result.discovered],
        errors=result.errors,
    )


@router.get(
    "/symbiotes/{symbiote_id}/discovered-tools",
    response_model=list[DiscoveredToolResponse],
)
def list_discovered_tools(
    symbiote_id: str,
    auth: Annotated[APIKey, Depends(require_auth)],
    identity: Annotated[IdentityManager, Depends(get_identity_manager)],
    adapter: Annotated[SQLiteAdapter, Depends(get_adapter)],
    status: str | None = Query(default=None),
) -> list[DiscoveredToolResponse]:
    """List tools discovered for a Symbiote, optionally filtered by status."""
    _require_symbiote(identity, symbiote_id, auth)

    repo = DiscoveredToolRepository(adapter)
    tools = repo.list(symbiote_id, status=status)
    return [_tool_to_response(t) for t in tools]


@router.patch(
    "/symbiotes/{symbiote_id}/discovered-tools/{tool_id}",
    response_model=DiscoveredToolResponse,
)
def update_discovered_tool(
    symbiote_id: str,
    tool_id: str,
    body: UpdateDiscoveredToolRequest,
    auth: Annotated[APIKey, Depends(require_auth)],
    identity: Annotated[IdentityManager, Depends(get_identity_manager)],
    adapter: Annotated[SQLiteAdapter, Depends(get_adapter)],
) -> DiscoveredToolResponse:
    """Update a discovered tool's status (approved / disabled / pending)."""
    if body.status not in ("approved", "disabled", "pending"):
        raise HTTPException(status_code=422, detail="status must be approved, disabled, or pending")

    _require_symbiote(identity, symbiote_id, auth)

    repo = DiscoveredToolRepository(adapter)
    updated = repo.set_status(symbiote_id, tool_id, body.status)
    if not updated:
        raise HTTPException(status_code=404, detail="Discovered tool not found")

    tool = repo.get(symbiote_id, tool_id)
    if tool is None:
        raise HTTPException(status_code=404, detail="Discovered tool not found")
    return _tool_to_response(tool)


@router.post(
    "/symbiotes/{symbiote_id}/discovered-tools/classify",
    response_model=ClassifyToolsResponse,
)
def classify_discovered_tools(
    symbiote_id: str,
    body: ClassifyToolsRequest,
    auth: Annotated[APIKey, Depends(require_auth)],
    identity: Annotated[IdentityManager, Depends(get_identity_manager)],
    adapter: Annotated[SQLiteAdapter, Depends(get_adapter)],
) -> ClassifyToolsResponse:
    """Batch approve/disable discovered tools by OpenAPI tags."""
    _require_symbiote(identity, symbiote_id, auth)

    repo = DiscoveredToolRepository(adapter)
    result = repo.classify_by_tags(
        symbiote_id, approve_tags=body.approve_tags, disable_rest=body.disable_rest
    )
    return ClassifyToolsResponse(**result)


@router.post(
    "/symbiotes/{symbiote_id}/discovered-tools/reset",
    response_model=ResetToolsResponse,
)
def reset_discovered_tools(
    symbiote_id: str,
    auth: Annotated[APIKey, Depends(require_auth)],
    identity: Annotated[IdentityManager, Depends(get_identity_manager)],
    adapter: Annotated[SQLiteAdapter, Depends(get_adapter)],
) -> ResetToolsResponse:
    """Reset all disabled discovered tools back to pending."""
    _require_symbiote(identity, symbiote_id, auth)

    repo = DiscoveredToolRepository(adapter)
    count = repo.reset_disabled(symbiote_id)
    return ResetToolsResponse(reset=count)


@router.delete("/symbiotes/{symbiote_id}/discovered-tools/{tool_id}", status_code=200)
def delete_discovered_tool(
    symbiote_id: str,
    tool_id: str,
    auth: Annotated[APIKey, Depends(require_auth)],
    identity: Annotated[IdentityManager, Depends(get_identity_manager)],
    adapter: Annotated[SQLiteAdapter, Depends(get_adapter)],
) -> dict:
    """Remove a discovered tool entry."""
    _require_symbiote(identity, symbiote_id, auth)

    repo = DiscoveredToolRepository(adapter)
    if not repo.delete(symbiote_id, tool_id):
        raise HTTPException(status_code=404, detail="Discovered tool not found")
    return {"removed": tool_id}


def _tool_to_response(tool: DiscoveredTool) -> DiscoveredToolResponse:
    return DiscoveredToolResponse(
        id=tool.id,
        tool_id=tool.tool_id,
        name=tool.name,
        description=tool.description,
        handler_type=tool.handler_type,
        method=tool.method,
        url_template=tool.url_template,
        parameters=tool.parameters,
        tags=tool.tags,
        risk_level=tool.risk_level,
        status=tool.status,
        source_path=tool.source_path,
        discovered_at=tool.discovered_at,
        approved_at=tool.approved_at,
    )
