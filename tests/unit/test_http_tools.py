"""Tests for HTTP API tool endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.api.http import app, get_adapter
from symbiote.core.identity import IdentityManager
from symbiote.environment.manager import EnvironmentManager


@pytest.fixture()
def adapter(tmp_path: Path, monkeypatch) -> SQLiteAdapter:
    # The API's kernel owns the only connection; tests use its storage.
    import symbiote.api.http as mod

    monkeypatch.setenv("SYMBIOTE_DB_PATH", str(tmp_path / "http_tools_test.db"))
    mod._kernel = mod._adapter = mod._tool_gateway = None
    yield mod.get_adapter()
    if mod._kernel is not None:
        mod._kernel.shutdown()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name="APIBot", role="assistant")
    return sym.id


@pytest.fixture()
def client(adapter: SQLiteAdapter) -> TestClient:
    # No ``with``: the lifespan would shut the kernel down at exit and the
    # adapter fixture does that already.
    yield TestClient(app)


class TestRegisterTool:
    def test_register_http_tool(self, client: TestClient, symbiote_id: str) -> None:
        resp = client.post(
            f"/symbiotes/{symbiote_id}/tools",
            json={
                "tool_id": "yn_search",
                "name": "Search",
                "description": "Search articles",
                "http_method": "GET",
                "url_template": "http://localhost:8000/api/search?q={q}",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["tool_id"] == "yn_search"
        assert data["handler_type"] == "http"

    def test_register_with_params_and_body(self, client: TestClient, symbiote_id: str) -> None:
        resp = client.post(
            f"/symbiotes/{symbiote_id}/tools",
            json={
                "tool_id": "yn_publish",
                "name": "Publish",
                "description": "Publish an article",
                "parameters": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
                "http_method": "POST",
                "url_template": "http://localhost:8000/api/items/{id}/publish",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["parameters"]["required"] == ["id"]


class TestListTools:
    def test_list_includes_registered(self, client: TestClient, symbiote_id: str) -> None:
        # Register a tool first
        client.post(
            f"/symbiotes/{symbiote_id}/tools",
            json={
                "tool_id": "yn_list",
                "name": "List",
                "description": "List items",
                "http_method": "GET",
                "url_template": "http://localhost:8000/api/items",
            },
        )
        resp = client.get(f"/symbiotes/{symbiote_id}/tools")
        assert resp.status_code == 200
        tools = resp.json()
        ids = [t["tool_id"] for t in tools]
        assert "yn_list" in ids

    def test_list_empty_for_new_symbiote(self, client: TestClient, symbiote_id: str) -> None:
        resp = client.get(f"/symbiotes/{symbiote_id}/tools")
        assert resp.status_code == 200
        assert resp.json() == []


class TestRemoveTool:
    def test_remove_registered_tool(self, client: TestClient, symbiote_id: str) -> None:
        client.post(
            f"/symbiotes/{symbiote_id}/tools",
            json={
                "tool_id": "yn_tmp",
                "name": "Temp",
                "description": "Temporary",
                "http_method": "GET",
                "url_template": "http://localhost:8000/tmp",
            },
        )
        resp = client.delete(f"/symbiotes/{symbiote_id}/tools/yn_tmp")
        assert resp.status_code == 200
        assert resp.json()["removed"] == "yn_tmp"

        # Verify it's gone from list
        resp = client.get(f"/symbiotes/{symbiote_id}/tools")
        ids = [t["tool_id"] for t in resp.json()]
        assert "yn_tmp" not in ids


class TestExecTool:
    def test_exec_builtin_authorized(self, client: TestClient, symbiote_id: str, tmp_path: Path) -> None:
        # Authorize fs_read

        env = EnvironmentManager(storage=get_adapter())
        env.configure(symbiote_id=symbiote_id, tools=["fs_read"])

        target = tmp_path / "test.txt"
        target.write_text("hello", encoding="utf-8")

        resp = client.post(
            f"/symbiotes/{symbiote_id}/tools/fs_read/exec",
            json={"params": {"path": str(target)}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["output"] == "hello"

    def test_exec_unauthorized_tool(self, client: TestClient, symbiote_id: str) -> None:
        resp = client.post(
            f"/symbiotes/{symbiote_id}/tools/fs_write/exec",
            json={"params": {"path": "/tmp/test", "content": "x"}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "blocked" in data["error"].lower() or "not allowed" in data["error"].lower()
