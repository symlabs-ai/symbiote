"""Tests for chat endpoint and API key auth — B-19, B-20."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.api.auth import APIKeyManager
from symbiote.api.http import app as fastapi_app
from symbiote.api.http import get_adapter, get_kernel
from symbiote.api.middleware import set_key_manager
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel


@pytest.fixture()
def setup(tmp_path: Path):
    """Set up test DB, kernel, and auth."""
    db = tmp_path / "api_test.db"
    adapter = SQLiteAdapter(db_path=db, check_same_thread=False)
    adapter.init_schema()

    # API key manager
    key_mgr = APIKeyManager(adapter)
    key_mgr.init_schema()
    set_key_manager(key_mgr)

    # Create admin key
    admin_key_obj, admin_raw = key_mgr.create_key("test-tenant", "Admin", role="admin")
    # Create user key
    user_key_obj, user_raw = key_mgr.create_key("test-tenant", "User", role="user")

    # Kernel with mock LLM — same DB path, will get its own adapter internally
    config = KernelConfig(db_path=db)
    llm = MockLLMAdapter(default_response="Mock LLM response.")
    kernel = SymbioteKernel(config=config, llm=llm)
    # Patch storage to be thread-safe for TestClient
    kernel._storage._conn.close()
    import sqlite3
    kernel._storage._conn = sqlite3.connect(str(db), check_same_thread=False)
    kernel._storage._conn.row_factory = sqlite3.Row
    kernel._storage._conn.execute("PRAGMA journal_mode=WAL")
    kernel._storage._conn.execute("PRAGMA foreign_keys=ON")

    # Override dependencies
    fastapi_app.dependency_overrides[get_adapter] = lambda: adapter
    fastapi_app.dependency_overrides[get_kernel] = lambda: kernel

    # Set module-level _key_manager in http.py for admin endpoints
    import symbiote.api.http as http_module
    http_module._key_manager = key_mgr

    client = TestClient(fastapi_app)

    yield {
        "client": client,
        "kernel": kernel,
        "admin_key": admin_raw,
        "user_key": user_raw,
        "adapter": adapter,
    }

    fastapi_app.dependency_overrides.clear()
    set_key_manager(None)
    kernel.shutdown()


class TestAuthMiddleware:
    def test_health_no_auth_required(self, setup) -> None:
        resp = setup["client"].get("/health")
        assert resp.status_code == 200

    def test_chat_without_key_returns_401(self, setup) -> None:
        # Create a symbiote and session first
        kernel = setup["kernel"]
        sym = kernel.create_symbiote(name="Bot", role="assistant")
        session = kernel.start_session(symbiote_id=sym.id)

        resp = setup["client"].post(
            f"/sessions/{session.id}/chat",
            json={"content": "Hello"},
        )
        assert resp.status_code in (401, 403)

    def test_chat_with_invalid_key_returns_401(self, setup) -> None:
        kernel = setup["kernel"]
        sym = kernel.create_symbiote(name="Bot", role="assistant")
        session = kernel.start_session(symbiote_id=sym.id)

        resp = setup["client"].post(
            f"/sessions/{session.id}/chat",
            json={"content": "Hello"},
            headers={"Authorization": "Bearer sk-symbiote_invalid"},
        )
        assert resp.status_code == 401

    def test_chat_with_valid_key_succeeds(self, setup) -> None:
        kernel = setup["kernel"]
        sym = kernel.create_symbiote(name="Bot", role="assistant")
        session = kernel.start_session(symbiote_id=sym.id)

        resp = setup["client"].post(
            f"/sessions/{session.id}/chat",
            json={"content": "Hello"},
            headers={"Authorization": f"Bearer {setup['user_key']}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "response" in data
        assert data["session_id"] == session.id


class TestChatEndpoint:
    def test_chat_returns_llm_response(self, setup) -> None:
        kernel = setup["kernel"]
        sym = kernel.create_symbiote(name="ChatBot", role="assistant")
        session = kernel.start_session(symbiote_id=sym.id)

        resp = setup["client"].post(
            f"/sessions/{session.id}/chat",
            json={"content": "What is Python?"},
            headers={"Authorization": f"Bearer {setup['user_key']}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "Mock LLM" in str(data["response"])

    def test_chat_invalid_session_returns_error(self, setup) -> None:
        resp = setup["client"].post(
            "/sessions/nonexistent/chat",
            json={"content": "Hello"},
            headers={"Authorization": f"Bearer {setup['user_key']}"},
        )
        assert resp.status_code in (400, 404)


class TestAPIKeyManagement:
    def test_create_key_admin_only(self, setup) -> None:
        resp = setup["client"].post(
            "/admin/api-keys",
            json={"tenant_id": "new-tenant", "name": "New Key"},
            headers={"Authorization": f"Bearer {setup['admin_key']}"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["raw_key"].startswith("sk-symbiote_")
        assert data["tenant_id"] == "new-tenant"

    def test_create_key_user_forbidden(self, setup) -> None:
        resp = setup["client"].post(
            "/admin/api-keys",
            json={"tenant_id": "t1", "name": "Nope"},
            headers={"Authorization": f"Bearer {setup['user_key']}"},
        )
        assert resp.status_code == 403

    def test_list_keys(self, setup) -> None:
        resp = setup["client"].get(
            "/admin/api-keys/test-tenant",
            headers={"Authorization": f"Bearer {setup['admin_key']}"},
        )
        assert resp.status_code == 200
        keys = resp.json()
        assert len(keys) >= 2  # admin + user keys created in setup

    def test_revoke_key(self, setup) -> None:
        # Create a key to revoke
        create_resp = setup["client"].post(
            "/admin/api-keys",
            json={"tenant_id": "t1", "name": "Revokable"},
            headers={"Authorization": f"Bearer {setup['admin_key']}"},
        )
        key_id = create_resp.json()["id"]

        resp = setup["client"].delete(
            f"/admin/api-keys/{key_id}",
            headers={"Authorization": f"Bearer {setup['admin_key']}"},
        )
        assert resp.status_code == 200
        assert resp.json()["revoked"] == key_id
