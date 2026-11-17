"""Tools from an OpenAPI description the host already has (B-88).

``discover_tools(url=...)`` only fetches ``{url}/openapi.json``. A host that
found the description somewhere else passes the parsed ``dict`` and gets the
same tools, stored, approved and executed the same way.
"""

from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.support import ScriptedLLM, make_kernel

SPEC = {
    "openapi": "3.0.0",
    "servers": [{"url": "http://from-servers.invalid/api/"}],
    "paths": {
        "/search": {
            "get": {
                "operationId": "search",
                "summary": "Search items",
                "parameters": [
                    {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}},
                ],
            }
        },
        "/items/{id}": {
            "delete": {
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ]
            }
        },
    },
}


class _Api(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def _go(self) -> None:
        if self.path == "/openapi.json":
            data = json.dumps(SPEC).encode()
        else:
            self.seen.append((self.command, self.path))
            data = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_DELETE = _go  # noqa: N815

    def log_message(self, *args) -> None:
        pass


@pytest.fixture()
def env(tmp_path: Path):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Api)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Api.seen = []
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    yield kernel, f"http://127.0.0.1:{srv.server_address[1]}"
    kernel.shutdown()
    srv.shutdown()


def _facts(tools) -> list[tuple]:
    return sorted(
        (t.tool_id, t.method, t.url_template, json.dumps(t.parameters, sort_keys=True),
         t.risk_level, t.name)
        for t in tools
    )


def test_spec_in_hand_gives_the_same_tools_as_the_fetched_one(env, tmp_path: Path) -> None:
    kernel, base = env
    by_url = kernel.create_symbiote(name="by-url", role="r").id
    by_spec = kernel.create_symbiote(name="by-spec", role="r").id
    empty = tmp_path / "src"
    empty.mkdir()

    fetched = kernel.discover_tools(by_url, str(empty), url=base)
    given = kernel.discover_tools(by_spec, spec=SPEC, base_url=base)

    assert given.errors == []
    assert len(given.discovered) == 2
    assert _facts(given.discovered) == _facts(fetched.discovered)
    assert {t.source_path for t in given.discovered} == {"openapi-spec"}
    assert all(t.status == "pending" for t in kernel.discovered_tools.list(by_spec))


def test_tools_from_a_spec_run_through_the_gateway(env) -> None:
    kernel, base = env
    sid = kernel.create_symbiote(name="a", role="r").id

    result = kernel.discover_tools(sid, "https://docs.example/openapi.yaml", spec=SPEC, base_url=base)
    for tool in result.discovered:
        kernel.discovered_tools.set_status(sid, tool.tool_id, "approved")
    kernel.load_discovered_tools(sid, base_url=base)

    outcome = kernel.tool_gateway.execute(sid, None, "search", {"q": "lamp"})

    assert outcome.success, outcome.error
    method, path = _Api.seen[-1]
    assert (method, urlsplit(path).path) == ("GET", "/search")
    assert parse_qs(urlsplit(path).query) == {"q": ["lamp"]}
    assert {t.source_path for t in result.discovered} == {"https://docs.example/openapi.yaml"}


def test_base_url_defaults_to_the_first_server_of_the_spec(env) -> None:
    kernel, _base = env
    sid = kernel.create_symbiote(name="a", role="r").id

    result = kernel.discover_tools(sid, spec=SPEC)

    urls = {t.tool_id: t.url_template for t in result.discovered}
    assert urls["search"].startswith("http://from-servers.invalid/api/search")


@pytest.mark.parametrize("bad", [{}, {"openapi": "3.0.0"}, ["not", "a", "dict"]])
def test_a_description_without_paths_is_an_error_not_an_exception(env, bad) -> None:
    kernel, _base = env
    sid = kernel.create_symbiote(name="a", role="r").id

    result = kernel.discover_tools(sid, spec=bad)

    assert result.discovered == []
    assert "invalid OpenAPI spec" in result.errors[0]
    assert kernel.discovered_tools.list(sid) == []
