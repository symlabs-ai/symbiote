"""Tests for Discovery API endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.api.http import app, get_adapter
from symbiote.api.middleware import set_key_manager
from symbiote.core.identity import IdentityManager


@pytest.fixture(autouse=True)
def dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable dev mode so auth passes without a real API key."""
    monkeypatch.setenv("SYMBIOTE_DEV_MODE", "1")
    set_key_manager(None)  # type: ignore[arg-type]


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "disc_api_test.db"
    adp = SQLiteAdapter(db_path=db, check_same_thread=False)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name="Clark", role="assistant", owner_id="default")
    return sym.id


@pytest.fixture()
def client(adapter: SQLiteAdapter) -> TestClient:
    import symbiote.api.http as mod

    mod._tool_gateway = None

    def _override() -> SQLiteAdapter:
        return adapter

    app.dependency_overrides[get_adapter] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    mod._tool_gateway = None


class TestDiscoverEndpoint:
    def test_discover_returns_found_tools(
        self, client: TestClient, symbiote_id: str, tmp_path: Path
    ) -> None:
        (tmp_path / "routes.py").write_text(
            '@router.get("/api/search")\ndef search(): pass\n'
            '@router.post("/api/publish")\ndef publish(): pass\n'
        )
        resp = client.post(
            f"/symbiotes/{symbiote_id}/discover",
            json={"source_path": str(tmp_path)},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["discovered"] == 2
        ids = [t["tool_id"] for t in data["tools"]]
        assert "get_api_search" in ids
        assert "post_api_publish" in ids

    def test_discover_tools_start_as_pending(
        self, client: TestClient, symbiote_id: str, tmp_path: Path
    ) -> None:
        (tmp_path / "routes.py").write_text('@app.get("/api/items")\ndef f(): pass\n')
        resp = client.post(
            f"/symbiotes/{symbiote_id}/discover",
            json={"source_path": str(tmp_path)},
        )
        assert all(t["status"] == "pending" for t in resp.json()["tools"])

    def test_discover_unknown_symbiote_404(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        resp = client.post(
            "/symbiotes/no-such-id/discover",
            json={"source_path": str(tmp_path)},
        )
        assert resp.status_code == 404


class TestListDiscoveredTools:
    def test_list_empty(self, client: TestClient, symbiote_id: str) -> None:
        resp = client.get(f"/symbiotes/{symbiote_id}/discovered-tools")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_after_discover(
        self, client: TestClient, symbiote_id: str, tmp_path: Path
    ) -> None:
        (tmp_path / "routes.py").write_text('@app.get("/api/search")\ndef f(): pass\n')
        client.post(
            f"/symbiotes/{symbiote_id}/discover",
            json={"source_path": str(tmp_path)},
        )
        resp = client.get(f"/symbiotes/{symbiote_id}/discovered-tools")
        assert len(resp.json()) == 1

    def test_list_filter_by_status(
        self, client: TestClient, symbiote_id: str, tmp_path: Path
    ) -> None:
        (tmp_path / "routes.py").write_text(
            '@app.get("/api/a")\ndef a(): pass\n'
            '@app.get("/api/b")\ndef b(): pass\n'
        )
        client.post(
            f"/symbiotes/{symbiote_id}/discover",
            json={"source_path": str(tmp_path)},
        )
        client.patch(
            f"/symbiotes/{symbiote_id}/discovered-tools/get_api_a",
            json={"status": "approved"},
        )
        pending = client.get(
            f"/symbiotes/{symbiote_id}/discovered-tools?status=pending"
        ).json()
        approved = client.get(
            f"/symbiotes/{symbiote_id}/discovered-tools?status=approved"
        ).json()
        assert len(pending) == 1
        assert len(approved) == 1


class TestUpdateDiscoveredTool:
    def test_approve_tool(
        self, client: TestClient, symbiote_id: str, tmp_path: Path
    ) -> None:
        (tmp_path / "routes.py").write_text('@app.get("/api/search")\ndef f(): pass\n')
        client.post(
            f"/symbiotes/{symbiote_id}/discover",
            json={"source_path": str(tmp_path)},
        )
        resp = client.patch(
            f"/symbiotes/{symbiote_id}/discovered-tools/get_api_search",
            json={"status": "approved"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"
        assert resp.json()["approved_at"] is not None

    def test_invalid_status_422(self, client: TestClient, symbiote_id: str) -> None:
        resp = client.patch(
            f"/symbiotes/{symbiote_id}/discovered-tools/any_tool",
            json={"status": "invalid"},
        )
        assert resp.status_code == 422

    def test_unknown_tool_404(self, client: TestClient, symbiote_id: str) -> None:
        resp = client.patch(
            f"/symbiotes/{symbiote_id}/discovered-tools/ghost",
            json={"status": "approved"},
        )
        assert resp.status_code == 404


class TestDeleteDiscoveredTool:
    def test_delete_tool(
        self, client: TestClient, symbiote_id: str, tmp_path: Path
    ) -> None:
        (tmp_path / "routes.py").write_text('@app.get("/api/search")\ndef f(): pass\n')
        client.post(
            f"/symbiotes/{symbiote_id}/discover",
            json={"source_path": str(tmp_path)},
        )
        resp = client.delete(
            f"/symbiotes/{symbiote_id}/discovered-tools/get_api_search"
        )
        assert resp.status_code == 200
        assert resp.json()["removed"] == "get_api_search"

    def test_delete_nonexistent_404(
        self, client: TestClient, symbiote_id: str
    ) -> None:
        resp = client.delete(
            f"/symbiotes/{symbiote_id}/discovered-tools/ghost"
        )
        assert resp.status_code == 404
