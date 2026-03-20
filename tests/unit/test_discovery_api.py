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


class TestClassifyEndpoint:
    """Tests for POST /symbiotes/{id}/discovered-tools/classify."""

    def _seed_tagged_tools(self, client, symbiote_id, tmp_path):
        """Discover tools from an OpenAPI spec with tags."""
        import json
        from unittest.mock import MagicMock, patch

        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0.0"},
            "paths": {
                "/api/items": {
                    "get": {
                        "operationId": "list_items",
                        "summary": "List items",
                        "tags": ["Items"],
                    }
                },
                "/api/compose": {
                    "post": {
                        "operationId": "create_compose",
                        "summary": "Create compose",
                        "tags": ["Compose"],
                    }
                },
                "/api/admin/config": {
                    "get": {
                        "operationId": "get_config",
                        "summary": "Get config",
                        "tags": ["Admin"],
                    }
                },
            },
        }
        resp = MagicMock()
        resp.read.return_value = json.dumps(spec).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=resp):
            client.post(
                f"/symbiotes/{symbiote_id}/discover",
                json={"source_path": str(tmp_path), "url": "http://localhost:8000"},
            )

    def test_classify_approves_matching_tags(
        self, client: TestClient, symbiote_id: str, tmp_path: Path
    ) -> None:
        self._seed_tagged_tools(client, symbiote_id, tmp_path)
        resp = client.post(
            f"/symbiotes/{symbiote_id}/discovered-tools/classify",
            json={"approve_tags": ["Items", "Compose"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["approved"] == 2
        assert data["disabled"] == 0

    def test_classify_with_disable_rest(
        self, client: TestClient, symbiote_id: str, tmp_path: Path
    ) -> None:
        self._seed_tagged_tools(client, symbiote_id, tmp_path)
        resp = client.post(
            f"/symbiotes/{symbiote_id}/discovered-tools/classify",
            json={"approve_tags": ["Items"], "disable_rest": True},
        )
        data = resp.json()
        assert data["approved"] == 1
        assert data["disabled"] == 2  # Compose + Admin

    def test_classify_unknown_symbiote_404(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/symbiotes/no-such-id/discovered-tools/classify",
            json={"approve_tags": ["Items"]},
        )
        assert resp.status_code == 404

    def test_classify_empty_tags_list(
        self, client: TestClient, symbiote_id: str, tmp_path: Path
    ) -> None:
        self._seed_tagged_tools(client, symbiote_id, tmp_path)
        resp = client.post(
            f"/symbiotes/{symbiote_id}/discovered-tools/classify",
            json={"approve_tags": [], "disable_rest": True},
        )
        data = resp.json()
        assert data["approved"] == 0
        assert data["disabled"] == 3


class TestResetEndpoint:
    """Tests for POST /symbiotes/{id}/discovered-tools/reset."""

    def test_reset_disabled_tools(
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
        # Disable one
        client.patch(
            f"/symbiotes/{symbiote_id}/discovered-tools/get_api_a",
            json={"status": "disabled"},
        )
        resp = client.post(
            f"/symbiotes/{symbiote_id}/discovered-tools/reset",
        )
        assert resp.status_code == 200
        assert resp.json()["reset"] == 1

    def test_reset_when_none_disabled(
        self, client: TestClient, symbiote_id: str
    ) -> None:
        resp = client.post(
            f"/symbiotes/{symbiote_id}/discovered-tools/reset",
        )
        assert resp.status_code == 200
        assert resp.json()["reset"] == 0

    def test_reset_unknown_symbiote_404(self, client: TestClient) -> None:
        resp = client.post(
            "/symbiotes/no-such-id/discovered-tools/reset",
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
