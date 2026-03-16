"""Tests for HTTP API — T-22."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.api.http import app, get_adapter
from symbiote.core.identity import IdentityManager
from symbiote.core.models import MemoryEntry
from symbiote.core.session import SessionManager
from symbiote.memory.store import MemoryStore


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "http_test.db"
    adp = SQLiteAdapter(db_path=db, check_same_thread=False)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def client(adapter: SQLiteAdapter) -> TestClient:
    """TestClient that overrides the DB dependency with a tmp_path adapter."""

    def _override_adapter() -> SQLiteAdapter:
        return adapter

    app.dependency_overrides[get_adapter] = _override_adapter
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── POST /symbiotes ───────────────────────────────────────────────────────


class TestCreateSymbiote:
    def test_create_symbiote_returns_201(self, client: TestClient) -> None:
        resp = client.post(
            "/symbiotes",
            json={"name": "Cody", "role": "coder"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert data["name"] == "Cody"
        assert data["role"] == "coder"
        assert data["status"] == "active"

    def test_create_symbiote_with_persona(self, client: TestClient) -> None:
        resp = client.post(
            "/symbiotes",
            json={
                "name": "Sage",
                "role": "advisor",
                "persona_json": {"tone": "formal"},
            },
        )
        assert resp.status_code == 201
        assert resp.json()["name"] == "Sage"


# ── GET /symbiotes/{id} ──────────────────────────────────────────────────


class TestGetSymbiote:
    def test_get_symbiote_returns_200(self, client: TestClient) -> None:
        create_resp = client.post(
            "/symbiotes",
            json={"name": "Bot", "role": "helper"},
        )
        sym_id = create_resp.json()["id"]

        resp = client.get(f"/symbiotes/{sym_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == sym_id
        assert data["name"] == "Bot"

    def test_get_symbiote_not_found_returns_404(
        self, client: TestClient
    ) -> None:
        resp = client.get("/symbiotes/nonexistent-id")
        assert resp.status_code == 404


# ── POST /sessions ────────────────────────────────────────────────────────


class TestCreateSession:
    def test_create_session_returns_201(self, client: TestClient) -> None:
        sym = client.post(
            "/symbiotes", json={"name": "S", "role": "r"}
        ).json()

        resp = client.post(
            "/sessions",
            json={"symbiote_id": sym["id"]},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert data["symbiote_id"] == sym["id"]
        assert data["status"] == "active"

    def test_create_session_with_goal(self, client: TestClient) -> None:
        sym = client.post(
            "/symbiotes", json={"name": "S", "role": "r"}
        ).json()

        resp = client.post(
            "/sessions",
            json={"symbiote_id": sym["id"], "goal": "Fix bug"},
        )
        assert resp.status_code == 201
        assert resp.json()["goal"] == "Fix bug"


# ── POST /sessions/{id}/messages ──────────────────────────────────────────


class TestAddMessage:
    def test_add_message_returns_201(self, client: TestClient) -> None:
        sym = client.post(
            "/symbiotes", json={"name": "S", "role": "r"}
        ).json()
        sess = client.post(
            "/sessions", json={"symbiote_id": sym["id"]}
        ).json()

        resp = client.post(
            f"/sessions/{sess['id']}/messages",
            json={"role": "user", "content": "Hello!"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["role"] == "user"
        assert data["content"] == "Hello!"
        assert "id" in data
        assert "created_at" in data

    def test_add_message_session_not_found_returns_404(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/sessions/nonexistent-id/messages",
            json={"role": "user", "content": "Hello!"},
        )
        assert resp.status_code == 404


# ── POST /sessions/{id}/close ────────────────────────────────────────────


class TestCloseSession:
    def test_close_session_returns_200(self, client: TestClient) -> None:
        sym = client.post(
            "/symbiotes", json={"name": "S", "role": "r"}
        ).json()
        sess = client.post(
            "/sessions", json={"symbiote_id": sym["id"]}
        ).json()

        resp = client.post(f"/sessions/{sess['id']}/close")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == sess["id"]
        assert data["status"] == "closed"
        assert "summary" in data

    def test_close_session_not_found_returns_404(
        self, client: TestClient
    ) -> None:
        resp = client.post("/sessions/nonexistent-id/close")
        assert resp.status_code == 404


# ── GET /sessions/{id} ───────────────────────────────────────────────────


class TestGetSession:
    def test_get_session_returns_200(self, client: TestClient) -> None:
        sym = client.post(
            "/symbiotes", json={"name": "S", "role": "r"}
        ).json()
        sess = client.post(
            "/sessions", json={"symbiote_id": sym["id"]}
        ).json()

        resp = client.get(f"/sessions/{sess['id']}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == sess["id"]
        assert data["symbiote_id"] == sym["id"]

    def test_get_session_not_found_returns_404(
        self, client: TestClient
    ) -> None:
        resp = client.get("/sessions/nonexistent-id")
        assert resp.status_code == 404


# ── GET /memory/search ───────────────────────────────────────────────────


class TestMemorySearch:
    def test_search_returns_200_empty_list(self, client: TestClient) -> None:
        resp = client.get("/memory/search", params={"query": "anything"})
        assert resp.status_code == 200
        assert resp.json() == []

    def test_search_returns_matching_entries(
        self, client: TestClient, adapter: SQLiteAdapter
    ) -> None:
        # Seed a symbiote and a memory entry directly
        sym = client.post(
            "/symbiotes", json={"name": "S", "role": "r"}
        ).json()

        store = MemoryStore(storage=adapter)
        entry = MemoryEntry(
            symbiote_id=sym["id"],
            type="factual",
            scope="global",
            content="Python is great",
            source="user",
        )
        store.store(entry)

        resp = client.get("/memory/search", params={"query": "Python"})
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) >= 1
        assert results[0]["content"] == "Python is great"

    def test_search_with_scope_filter(
        self, client: TestClient, adapter: SQLiteAdapter
    ) -> None:
        sym = client.post(
            "/symbiotes", json={"name": "S", "role": "r"}
        ).json()

        store = MemoryStore(storage=adapter)
        entry = MemoryEntry(
            symbiote_id=sym["id"],
            type="factual",
            scope="project",
            content="Rust is fast",
            source="user",
        )
        store.store(entry)

        resp = client.get(
            "/memory/search",
            params={"query": "Rust", "scope": "project"},
        )
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

        # Different scope should not match
        resp2 = client.get(
            "/memory/search",
            params={"query": "Rust", "scope": "session"},
        )
        assert resp2.status_code == 200
        assert len(resp2.json()) == 0

    def test_search_respects_limit(
        self, client: TestClient, adapter: SQLiteAdapter
    ) -> None:
        sym = client.post(
            "/symbiotes", json={"name": "S", "role": "r"}
        ).json()

        store = MemoryStore(storage=adapter)
        for i in range(5):
            entry = MemoryEntry(
                symbiote_id=sym["id"],
                type="factual",
                scope="global",
                content=f"fact number {i}",
                source="user",
            )
            store.store(entry)

        resp = client.get(
            "/memory/search",
            params={"query": "fact", "limit": 2},
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 2
