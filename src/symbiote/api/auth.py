"""API Key authentication for hosted Symbiote service.

Keys are stored in the same SQLite DB. Each key is scoped to a tenant
and carries a role (admin, user). The middleware extracts the key from
the Authorization header and injects the tenant_id into the request state.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel

from symbiote.core.ports import StoragePort

_API_KEYS_SCHEMA = """\
CREATE TABLE IF NOT EXISTS api_keys (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    key_hash    TEXT NOT NULL UNIQUE,
    key_prefix  TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'user',
    is_active   INTEGER DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_tenant ON api_keys(tenant_id);
"""


class APIKey(BaseModel):
    id: str
    tenant_id: str
    name: str
    key_prefix: str
    role: str
    is_active: bool
    created_at: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class APIKeyManager:
    """Manages API key lifecycle: create, validate, revoke."""

    KEY_PREFIX = "sk-symbiote_"
    VALID_ROLES = frozenset({"user", "admin"})

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage

    def init_schema(self) -> None:
        """Create api_keys table if not exists."""
        for statement in _API_KEYS_SCHEMA.split(";"):
            stmt = statement.strip()
            if stmt:
                self._storage.execute(stmt)

    def create_key(self, tenant_id: str, name: str, role: str = "user") -> tuple[APIKey, str]:
        """Create a new API key.

        Returns (APIKey metadata, raw_key). The raw key is only shown once.

        Raises:
            ValueError: If role is not one of VALID_ROLES.
        """
        if role not in self.VALID_ROLES:
            raise ValueError(f"Invalid role '{role}'. Must be one of: {sorted(self.VALID_ROLES)}")
        raw_key = f"{self.KEY_PREFIX}{secrets.token_urlsafe(32)}"
        key_hash = self._hash_key(raw_key)
        key_prefix = raw_key[:18]
        key_id = str(uuid4())
        now = datetime.now(tz=UTC).isoformat()

        self._storage.execute(
            "INSERT INTO api_keys (id, tenant_id, name, key_hash, key_prefix, role, is_active, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (key_id, tenant_id, name, key_hash, key_prefix, role, now),
        )

        api_key = APIKey(
            id=key_id,
            tenant_id=tenant_id,
            name=name,
            key_prefix=key_prefix,
            role=role,
            is_active=True,
            created_at=now,
        )
        return api_key, raw_key

    def validate_key(self, raw_key: str) -> APIKey | None:
        """Validate a raw API key. Returns APIKey if valid, None otherwise."""
        key_hash = self._hash_key(raw_key)
        row = self._storage.fetch_one(
            "SELECT * FROM api_keys WHERE key_hash = ? AND is_active = 1",
            (key_hash,),
        )
        if row is None:
            return None
        return APIKey(
            id=row["id"],
            tenant_id=row["tenant_id"],
            name=row["name"],
            key_prefix=row["key_prefix"],
            role=row["role"],
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
        )

    def revoke_key(self, key_id: str) -> bool:
        """Revoke an API key by ID. Returns True if found."""
        row = self._storage.fetch_one(
            "SELECT id FROM api_keys WHERE id = ?", (key_id,)
        )
        if row is None:
            return False
        self._storage.execute(
            "UPDATE api_keys SET is_active = 0 WHERE id = ?", (key_id,)
        )
        return True

    def list_keys(self, tenant_id: str) -> list[APIKey]:
        """List all keys for a tenant."""
        rows = self._storage.fetch_all(
            "SELECT * FROM api_keys WHERE tenant_id = ? ORDER BY created_at DESC",
            (tenant_id,),
        )
        return [
            APIKey(
                id=r["id"],
                tenant_id=r["tenant_id"],
                name=r["name"],
                key_prefix=r["key_prefix"],
                role=r["role"],
                is_active=bool(r["is_active"]),
                created_at=r["created_at"],
            )
            for r in rows
        ]

    @staticmethod
    def _hash_key(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode()).hexdigest()
