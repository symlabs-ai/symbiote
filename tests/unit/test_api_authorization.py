"""Regression: every API route that touches data requires a key and checks
ownership (report item S-2)."""

from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute

from symbiote.api.http import app

PUBLIC_PATHS = {"/health", "/", "/docs", "/openapi.json", "/redoc"}


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def _protected_routes() -> list[tuple[str, str]]:
    out = []
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path not in PUBLIC_PATHS:
            for method in sorted(route.methods):
                out.append((method, route.path))
    return out


def test_route_sweep_finds_routes() -> None:
    # Guards the sweep below against silently testing nothing.
    assert len(_protected_routes()) > 40


@pytest.mark.parametrize(("method", "path"), _protected_routes())
def test_every_non_public_route_requires_a_key(api_client, method, path) -> None:
    api = api_client()
    kwargs = {"json": {}} if method in {"POST", "PUT", "PATCH"} else {}

    resp = api.client.request(method, _concrete(path), **kwargs)

    assert resp.status_code == 401, (method, path, resp.status_code, resp.text[:200])


def test_public_routes_stay_public(api_client) -> None:
    api = api_client()
    assert api.client.get("/health").status_code == 200
    assert api.client.get("/").status_code == 200


@pytest.fixture()
def tenants(api_client):
    api = api_client()
    key_a = api.new_key("tenant-a")
    key_b = api.new_key("tenant-b")
    c = api.client
    sym = c.post("/symbiotes", headers=api.auth(key_a), json={"name": "A", "role": "r"}).json()
    sess = c.post(
        "/sessions",
        headers=api.auth(key_a),
        json={"symbiote_id": sym["id"], "goal": "secret goal", "external_key": "a:user1"},
    ).json()
    c.post(
        f"/sessions/{sess['id']}/messages",
        headers=api.auth(key_a),
        json={"role": "user", "content": "secret of tenant A"},
    )
    return api, key_a, key_b, sym["id"], sess["id"]


def _cross_tenant_requests(sym: str, sess: str) -> list[tuple[str, str, dict]]:
    tool = {
        "tool_id": "probe",
        "name": "p",
        "description": "d",
        "parameters": {"type": "object", "properties": {}},
        "http_method": "GET",
        "url_template": "http://example.invalid/x",
    }
    return [
        ("GET", f"/symbiotes/{sym}", {}),
        ("PUT", f"/symbiotes/{sym}", {"json": {"role": "x"}}),
        ("DELETE", f"/symbiotes/{sym}", {}),
        ("GET", f"/symbiotes/{sym}/config", {}),
        ("PUT", f"/symbiotes/{sym}/config", {"json": {"tool_mode": "instant"}}),
        ("GET", f"/symbiotes/{sym}/tool-tags", {}),
        ("PUT", f"/symbiotes/{sym}/tool-tags", {"json": {"tags": []}}),
        ("GET", f"/symbiotes/{sym}/discovered-tools", {}),
        ("POST", "/sessions", {"json": {"symbiote_id": sym}}),
        ("GET", f"/sessions/{sess}", {}),
        ("GET", "/sessions/by-key/a:user1", {}),
        ("POST", f"/sessions/{sess}/messages", {"json": {"role": "user", "content": "x"}}),
        ("POST", f"/sessions/{sess}/close", {}),
        ("POST", f"/sessions/{sess}/chat", {"json": {"content": "hi"}}),
        ("GET", "/memory/search", {"params": {"query": "secret", "symbiote_id": sym}}),
        ("POST", f"/symbiotes/{sym}/tools", {"json": tool}),
        ("GET", f"/symbiotes/{sym}/tools", {}),
        ("DELETE", f"/symbiotes/{sym}/tools/probe", {}),
        ("POST", f"/symbiotes/{sym}/tools/probe/exec", {"json": {"params": {}}}),
    ]


def test_other_tenant_gets_403_everywhere(tenants) -> None:
    api, _key_a, key_b, sym, sess = tenants
    failures = []
    for method, path, kwargs in _cross_tenant_requests(sym, sess):
        resp = api.client.request(method, path, headers=api.auth(key_b), **kwargs)
        if resp.status_code != 403:
            failures.append((method, path, resp.status_code))
    assert failures == []


def test_owner_can_use_own_resources(tenants) -> None:
    api, key_a, _key_b, sym, sess = tenants
    c, h = api.client, api.auth(key_a)
    assert c.get(f"/sessions/{sess}", headers=h).status_code == 200
    assert c.get("/sessions/by-key/a:user1", headers=h).status_code == 200
    assert c.get(f"/symbiotes/{sym}/tools", headers=h).status_code == 200
    assert c.get("/memory/search", headers=h, params={"query": "x", "symbiote_id": sym}).status_code == 200


@pytest.mark.parametrize(
    "path_tpl",
    [
        "/api/dashboard",
        "/api/logs",
        "/api/symbiotes",
        "/api/sessions",
        "/api/sessions/{sess}/messages",
        "/api/sessions/{sess}/trace",
        "/api/sessions/{sess}/score",
        "/api/symbiotes/{sym}/memory",
        "/api/symbiotes/{sym}/activity",
        "/api/symbiotes/{sym}/harness",
    ],
)
def test_console_data_routes_are_admin_only(tenants, path_tpl) -> None:
    api, key_a, _key_b, sym, sess = tenants
    path = path_tpl.format(sym=sym, sess=sess)

    assert api.client.get(path, headers=api.auth(key_a)).status_code == 403
    assert api.client.get(path, headers=api.auth(api.admin_key)).status_code == 200


def test_ownerless_symbiote_is_admin_only(api_client) -> None:
    api = api_client()
    orphan = api.kernel.create_symbiote(name="orphan", role="r")  # owner_id=None
    user = api.new_key("tenant-a")

    assert api.client.get(f"/symbiotes/{orphan.id}", headers=api.auth(user)).status_code == 403
    assert (
        api.client.get(f"/symbiotes/{orphan.id}", headers=api.auth(api.admin_key)).status_code
        == 200
    )


def test_deleted_symbiote_is_gone(tenants) -> None:
    api, key_a, _key_b, sym, sess = tenants
    c, h = api.client, api.auth(key_a)
    assert c.delete(f"/symbiotes/{sym}", headers=h).status_code == 204

    assert c.post("/sessions", headers=h, json={"symbiote_id": sym}).status_code == 404
    assert c.get(f"/sessions/{sess}", headers=h).status_code == 404
    admin = api.auth(api.admin_key)
    assert c.get(f"/api/sessions/{sess}/messages", headers=admin).status_code == 404


def test_session_for_unknown_symbiote_is_404(api_client) -> None:
    api = api_client()
    key = api.new_key("tenant-a")
    resp = api.client.post("/sessions", headers=api.auth(key), json={"symbiote_id": "nope"})
    assert resp.status_code == 404
