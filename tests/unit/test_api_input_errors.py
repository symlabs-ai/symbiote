"""Regression: invalid input gets a 4xx, never a 500 (report item A-1)."""

from __future__ import annotations

import pytest


@pytest.fixture()
def owned(api_client):
    api = api_client()
    key = api.new_key("tenant-a")
    h = api.auth(key)
    sym = api.client.post("/symbiotes", headers=h, json={"name": "A", "role": "r"}).json()
    sess = api.client.post("/sessions", headers=h, json={"symbiote_id": sym["id"]}).json()
    return api, h, sym["id"], sess["id"]


def test_message_with_invalid_role(owned) -> None:
    api, h, _sym, sess = owned
    resp = api.client.post(
        f"/sessions/{sess}/messages", headers=h, json={"role": "banana", "content": "x"}
    )
    assert resp.status_code == 422


def test_session_for_missing_symbiote(owned) -> None:
    api, h, _sym, _sess = owned
    resp = api.client.post("/sessions", headers=h, json={"symbiote_id": "does-not-exist"})
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"max_tool_iterations": -5},
        {"max_tool_iterations": 99999},
        {"tool_mode": "banana"},
        {"memory_share": 5},
    ],
)
def test_invalid_config_values(owned, body) -> None:
    api, h, sym, _sess = owned
    resp = api.client.put(f"/symbiotes/{sym}/config", headers=h, json=body)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]


def test_api_key_with_invalid_role(owned) -> None:
    api, _h, _sym, _sess = owned
    resp = api.client.post(
        "/admin/api-keys",
        headers=api.auth(api.admin_key),
        json={"tenant_id": "t", "name": "n", "role": "root"},
    )
    assert resp.status_code == 422


def test_integrity_error_maps_to_409(owned) -> None:
    from fastapi.testclient import TestClient

    from symbiote.api.http import app, get_adapter

    @app.get("/__test_integrity_error")
    def _boom() -> None:
        # A real constraint violation, raised by whatever adapter is in use.
        for _ in range(2):
            get_adapter().execute(
                "INSERT INTO symbiotes (id, name) VALUES (?, ?)", ("dup-409", "x")
            )

    try:
        api, h, _sym, _sess = owned
        resp = TestClient(app).get("/__test_integrity_error", headers=h)
        assert resp.status_code == 409
    finally:
        app.router.routes[:] = [
            r for r in app.router.routes if getattr(r, "path", "") != "/__test_integrity_error"
        ]
