"""Tests for API key authentication — B-19."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.api.auth import APIKeyManager


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "auth_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def manager(adapter: SQLiteAdapter) -> APIKeyManager:
    mgr = APIKeyManager(adapter)
    mgr.init_schema()
    return mgr


class TestAPIKeyManager:
    def test_create_key_returns_raw_key(self, manager: APIKeyManager) -> None:
        api_key, raw_key = manager.create_key("tenant-1", "Test Key")
        assert raw_key.startswith("sk-symbiote_")
        assert api_key.tenant_id == "tenant-1"
        assert api_key.name == "Test Key"
        assert api_key.role == "user"
        assert api_key.is_active is True

    def test_validate_valid_key(self, manager: APIKeyManager) -> None:
        _, raw_key = manager.create_key("t1", "Key1")
        result = manager.validate_key(raw_key)
        assert result is not None
        assert result.tenant_id == "t1"

    def test_validate_invalid_key(self, manager: APIKeyManager) -> None:
        result = manager.validate_key("sk-symbiote_invalid_key_here")
        assert result is None

    def test_validate_revoked_key(self, manager: APIKeyManager) -> None:
        api_key, raw_key = manager.create_key("t1", "Revokable")
        manager.revoke_key(api_key.id)
        result = manager.validate_key(raw_key)
        assert result is None

    def test_revoke_returns_true(self, manager: APIKeyManager) -> None:
        api_key, _ = manager.create_key("t1", "ToRevoke")
        assert manager.revoke_key(api_key.id) is True

    def test_revoke_nonexistent_returns_false(self, manager: APIKeyManager) -> None:
        assert manager.revoke_key("nonexistent-id") is False

    def test_list_keys_by_tenant(self, manager: APIKeyManager) -> None:
        manager.create_key("tenant-a", "Key A1")
        manager.create_key("tenant-a", "Key A2")
        manager.create_key("tenant-b", "Key B1")

        keys_a = manager.list_keys("tenant-a")
        keys_b = manager.list_keys("tenant-b")

        assert len(keys_a) == 2
        assert len(keys_b) == 1

    def test_create_admin_key(self, manager: APIKeyManager) -> None:
        api_key, _ = manager.create_key("t1", "Admin", role="admin")
        assert api_key.role == "admin"

    def test_key_prefix_stored(self, manager: APIKeyManager) -> None:
        api_key, raw_key = manager.create_key("t1", "Prefixed")
        assert api_key.key_prefix == raw_key[:18]

    def test_key_hash_is_deterministic(self, manager: APIKeyManager) -> None:
        """Same raw key always produces same hash."""
        h1 = APIKeyManager._hash_key("test-key")
        h2 = APIKeyManager._hash_key("test-key")
        assert h1 == h2
        assert h1 != APIKeyManager._hash_key("different-key")
