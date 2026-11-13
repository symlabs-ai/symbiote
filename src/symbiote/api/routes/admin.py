"""API key management (admin only)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from symbiote.api import http as _api
from symbiote.api.auth import APIKey
from symbiote.api.limits import (
    MAX_NAME_CHARS,
)
from symbiote.api.middleware import (
    require_admin,
)

router = APIRouter()


# ══════════════════════════════════════════════════════════════════════════
# API KEY MANAGEMENT (B-19)
# ══════════════════════════════════════════════════════════════════════════


class CreateAPIKeyRequest(BaseModel):
    tenant_id: str = Field(max_length=MAX_NAME_CHARS)
    name: str = Field(max_length=MAX_NAME_CHARS)
    role: Literal["user", "admin"] = "user"


class APIKeyResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    key_prefix: str
    role: str
    raw_key: str | None = None  # Only returned on create


@router.post("/admin/api-keys", status_code=201, response_model=APIKeyResponse)
def create_api_key(
    body: CreateAPIKeyRequest,
    auth: Annotated[APIKey, Depends(require_admin)],
) -> APIKeyResponse:
    """Create a new API key (admin only)."""
    if _api._key_manager is None:
        raise HTTPException(status_code=500, detail="Key manager not initialized")
    api_key, raw_key = _api._key_manager.create_key(
        tenant_id=body.tenant_id, name=body.name, role=body.role
    )
    return APIKeyResponse(
        id=api_key.id,
        tenant_id=api_key.tenant_id,
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        role=api_key.role,
        raw_key=raw_key,
    )


@router.get("/admin/api-keys/{tenant_id}", response_model=list[APIKeyResponse])
def list_api_keys(
    tenant_id: str,
    auth: Annotated[APIKey, Depends(require_admin)],
) -> list[APIKeyResponse]:
    """List API keys for a tenant (admin only)."""
    if _api._key_manager is None:
        raise HTTPException(status_code=500, detail="Key manager not initialized")
    keys = _api._key_manager.list_keys(tenant_id)
    return [
        APIKeyResponse(
            id=k.id, tenant_id=k.tenant_id, name=k.name,
            key_prefix=k.key_prefix, role=k.role,
        )
        for k in keys
    ]


@router.delete("/admin/api-keys/{key_id}", status_code=200)
def revoke_api_key(
    key_id: str,
    auth: Annotated[APIKey, Depends(require_admin)],
) -> dict:
    """Revoke an API key (admin only)."""
    if _api._key_manager is None:
        raise HTTPException(status_code=500, detail="Key manager not initialized")
    if not _api._key_manager.revoke_key(key_id):
        raise HTTPException(status_code=404, detail="API key not found")
    return {"revoked": key_id}
