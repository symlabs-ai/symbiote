"""Regression: discovered tools send their query parameters, routes that differ
only by path parameters get distinct tool ids, and path converters work
(report item T-2)."""

from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from symbiote.discovery.loader import DiscoveredToolLoader
from symbiote.discovery.repository import DiscoveredToolRepository
from symbiote.discovery.service import DiscoveryService
from tests.support import ScriptedLLM, make_kernel

SPEC = {
    "openapi": "3.0.0",
    "paths": {
        "/search": {
            "get": {
                "operationId": "search",
                "parameters": [
                    {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                ],
            }
        },
        "/items": {"get": {}, "post": {
            "parameters": [{"name": "dry_run", "in": "query", "schema": {"type": "boolean"}}],
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object", "properties": {"title": {"type": "string"}},
                "required": ["title"]}}}},
        }},
        "/items/{id}": {"get": {"parameters": [
            {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}]}},
        "/items/{id}/{rev}": {"get": {"parameters": [
            {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
            {"name": "rev", "in": "path", "required": True, "schema": {"type": "string"}}]}},
        "/files/{file_path:path}": {"get": {"parameters": [
            {"name": "file_path", "in": "path", "required": True, "schema": {"type": "string"}}]}},
        "/dup-a": {"get": {"operationId": "same"}},
        "/dup-b": {"get": {"operationId": "same"}},
    },
}


class _Api(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def _go(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        if self.path == "/openapi.json":
            data = json.dumps(SPEC).encode()
        else:
            self.seen.append((self.command, self.path, body))
            data = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _go  # noqa: N815

    def log_message(self, *args) -> None:
        pass


@pytest.fixture()
def env(tmp_path: Path):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Api)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Api.seen = []
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sid = kernel.create_symbiote(name="a", role="r").id
    repo = DiscoveredToolRepository(kernel._storage)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    empty = tmp_path / "src"
    empty.mkdir()
    result = DiscoveryService(repo).discover(sid, str(empty), url=base)
    for tool in result.discovered:
        repo.set_status(sid, tool.tool_id, "approved")
    loaded = DiscoveredToolLoader(repo, kernel.tool_gateway).load(sid, base_url=base)
    kernel.environment.configure(symbiote_id=sid, tools=loaded)
    yield kernel, sid, result
    kernel.shutdown()
    srv.shutdown()


def _call(kernel, sid, tool_id, **params):
    res = kernel.tool_gateway.execute(sid, None, tool_id, params)
    assert res.success, res.error
    return _Api.seen[-1]


def test_query_parameters_are_sent(env) -> None:
    kernel, sid, _ = env
    _method, path, _ = _call(kernel, sid, "search", q="a b&c", limit=5)
    assert urlsplit(path).path == "/search"
    assert parse_qs(urlsplit(path).query) == {"q": ["a b&c"], "limit": ["5"]}

    _method, path, _ = _call(kernel, sid, "search", q="x")
    assert parse_qs(urlsplit(path).query) == {"q": ["x"]}


def test_post_query_param_stays_out_of_the_body(env) -> None:
    kernel, sid, result = env
    post = next(t for t in result.discovered if t.method == "POST")
    method, path, body = _call(kernel, sid, post.tool_id, title="t", dry_run=True)
    assert method == "POST"
    assert parse_qs(urlsplit(path).query) == {"dry_run": ["True"]}
    assert json.loads(body) == {"title": "t"}


def test_routes_differing_by_path_params_get_distinct_ids(env) -> None:
    _kernel, _sid, result = env
    ids = [t.tool_id for t in result.discovered]
    assert len(ids) == len(set(ids))
    assert len([tid for tid in ids if tid.startswith("get_items")]) == 3, ids


def test_path_converter_route_works(env) -> None:
    kernel, sid, result = env
    tool = next(t for t in result.discovered if "/files/" in (t.url_template or ""))
    assert ":path" not in tool.url_template
    _method, path, _ = _call(kernel, sid, tool.tool_id, file_path="docs/a.txt")
    assert path.startswith("/files/docs")


def test_tool_id_collision_is_reported(env) -> None:
    _kernel, _sid, result = env
    assert [t.tool_id for t in result.discovered].count("same") == 1
    assert any("same" in e for e in result.errors)


def test_flask_and_fastapi_routes_normalise_converters(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sid = kernel.create_symbiote(name="a", role="r").id
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text(
        '@app.get("/files/{file_path:path}")\ndef f(): ...\n'
        '@app.route("/users/<int:user_id>", methods=["GET"])\ndef g(): ...\n'
    )
    result = DiscoveryService(DiscoveredToolRepository(kernel._storage)).discover(sid, str(src))
    templates = sorted(t.url_template for t in result.discovered)
    assert templates == ["{base_url}/files/{file_path}", "{base_url}/users/{user_id}"]
    kernel.shutdown()
