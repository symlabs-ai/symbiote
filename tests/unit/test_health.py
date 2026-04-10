"""Tests for GET /health endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.api.http import app, get_adapter


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "health_test.db"
    adp = SQLiteAdapter(db_path=db, check_same_thread=False)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def client(adapter: SQLiteAdapter) -> TestClient:
    def _override_adapter() -> SQLiteAdapter:
        return adapter

    app.dependency_overrides[get_adapter] = _override_adapter
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_health_status_ok(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health_returns_service_field(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.json()["service"] == "symbiote"


def test_health_returns_version_field(client: TestClient) -> None:
    resp = client.get("/health")
    assert "version" in resp.json()


def test_health_returns_commit_field(client: TestClient) -> None:
    resp = client.get("/health")
    assert "commit" in resp.json()
