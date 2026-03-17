"""Authentication middleware for the Symbiote HTTP API."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

if TYPE_CHECKING:
    from symbiote.api.auth import APIKey, APIKeyManager

_security = HTTPBearer(auto_error=False)

# Module-level reference, set during app startup
_key_manager: APIKeyManager | None = None

# Paths that don't require authentication
_PUBLIC_PATHS = frozenset({"/health", "/docs", "/openapi.json", "/redoc"})


def set_key_manager(manager: APIKeyManager) -> None:
    """Set the global key manager (called during app startup)."""
    global _key_manager
    _key_manager = manager


async def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Security(_security),  # noqa: B008
) -> APIKey:
    """FastAPI dependency that validates the API key.

    Injects the validated APIKey into the route handler.
    """
    if _key_manager is None:
        # Auth not configured — only allow in explicit dev mode
        if os.environ.get("SYMBIOTE_DEV_MODE") == "1":
            from symbiote.api.auth import APIKey

            return APIKey(
                id="dev",
                tenant_id="default",
                name="development",
                key_prefix="dev",
                role="admin",
                is_active=True,
                created_at="",
            )
        raise HTTPException(
            status_code=503, detail="Authentication not configured"
        )

    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Missing API key. Use Authorization: Bearer sk-symbiote_...",
        )

    api_key = _key_manager.validate_key(credentials.credentials)
    if api_key is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")

    return api_key


async def require_admin(
    api_key: APIKey = Depends(require_auth),  # noqa: B008
) -> APIKey:
    """Require admin role."""
    if api_key.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return api_key
