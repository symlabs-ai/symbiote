"""Reading an OpenAPI description without storing or authorizing (B-98).

``discover_tools(spec=...)`` stores the operations as tools of one symbiote,
and ``load_discovered_tools`` authorizes them there, with one credential for
the whole load. A host that manages integrations with other people's APIs
needs the two steps alone: ``parse_openapi`` reads the description and
``http_config_for`` builds the configuration of one operation. The host then
registers each tool under its own id, with its own credential.
"""

from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path

import pytest

import symbiote.discovery as discovery
from symbiote.core.exceptions import ValidationError
from symbiote.discovery import OpenAPIOperation, http_config_for, parse_openapi
from symbiote.discovery.repository import DiscoveredToolRepository
from symbiote.environment.descriptors import HttpToolConfig, ToolDescriptor
from tests.support import ScriptedLLM, make_kernel

SPEC = {
    "openapi": "3.0.0",
    "servers": [{"url": "https://api.example.com/v2/"}],
    "paths": {
        "/search": {"get": {
            "operationId": "search",
            "summary": "Search the catalogue",
            "description": "Full-text search over the catalogue.",
            "tags": ["catalogue"],
            "parameters": [
                {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}},
                {"name": "limit", "in": "query", "schema": {"type": "integer"}},
            ],
        }},
        "/items/{id}": {
            "delete": {"parameters": [
                {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}]},
            "put": {
                "operationId": "Replace Item",
                "x-risk-level": "high",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "dry_run", "in": "query", "schema": {"type": "boolean"}},
                ],
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "labels": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["title"],
                }}}},
            },
        },
    },
}


def _by_id(operations: list[OpenAPIOperation]) -> dict[str, OpenAPIOperation]:
    return {op.tool_id: op for op in operations}


# ── parse_openapi ───────────────────────────────────────────────────────


def test_one_entry_per_operation_with_what_a_host_needs_to_register_it() -> None:
    ops = _by_id(parse_openapi(SPEC))

    assert set(ops) == {"search", "delete_items_by_id", "replace_item"}
    search = ops["search"]
    assert search.operation_id == "search"
    assert search.name == "Search the catalogue"
    assert search.description == "Full-text search over the catalogue."
    assert (search.method, search.risk_level, search.tags) == ("GET", "low", ["catalogue"])
    assert search.path == "/search?q={q}&limit={limit}"
    assert search.url_template == "https://api.example.com/v2/search?q={q}&limit={limit}"
    assert set(search.parameters["properties"]) == {"q", "limit"}
    assert search.parameters["required"] == ["q"]

    delete = ops["delete_items_by_id"]  # no operationId: named after method and path
    assert (delete.operation_id, delete.method, delete.risk_level) == (None, "DELETE", "high")
    assert delete.name == "DELETE /items/{id}"

    replace = ops["replace_item"]
    assert (replace.operation_id, replace.risk_level) == ("Replace Item", "high")
    assert set(replace.parameters["properties"]) == {"id", "dry_run", "title", "labels"}


def test_base_url_is_the_first_server_unless_the_host_says_another() -> None:
    assert parse_openapi(SPEC)[0].url_template.startswith("https://api.example.com/v2/search")
    assert parse_openapi(SPEC, "https://proxy.test/")[0].url_template.startswith(
        "https://proxy.test/search"
    )
    no_servers = {"paths": SPEC["paths"]}
    (search,) = [op for op in parse_openapi(no_servers) if op.tool_id == "search"]
    assert search.url_template == search.path == "/search?q={q}&limit={limit}"


def test_it_reads_only(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    try:
        sym = kernel.create_symbiote(name="a", role="r")
        registered = {d.tool_id for d in kernel.tool_gateway.get_descriptors()}

        parse_openapi(SPEC)

        assert DiscoveredToolRepository(kernel._storage).list(sym.id) == []
        assert {d.tool_id for d in kernel.tool_gateway.get_descriptors()} == registered
        assert kernel.environment.list_tools(sym.id) == []
    finally:
        kernel.shutdown()


def test_an_operation_is_a_value_that_cannot_be_changed() -> None:
    (op, *_rest) = parse_openapi(SPEC)
    with pytest.raises(Exception, match="frozen"):
        op.tool_id = "other"


def test_malformed_operations_are_skipped_and_reported() -> None:
    spec = {"paths": {"/ok": {"get": {}}, "/null": None, "/bad": {"post": "nope"}}}
    errors: list[str] = []

    ops = parse_openapi(spec, errors=errors)

    assert [op.tool_id for op in ops] == ["get_ok"]
    assert len(errors) == 2 and "/null" in errors[0] and "/bad" in errors[1]
    assert [op.tool_id for op in parse_openapi(spec)] == ["get_ok"]  # errors are optional


@pytest.mark.parametrize("bad", [None, "openapi: 3.0.0", [], {"info": {}}])
def test_what_is_not_an_openapi_description_is_refused(bad) -> None:
    with pytest.raises(ValidationError, match="OpenAPI"):
        parse_openapi(bad)


def test_it_is_the_conversion_discover_tools_stores(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    try:
        sym = kernel.create_symbiote(name="a", role="r")
        kernel.discover_tools(sym.id, spec=SPEC)
        stored = {t.tool_id: t for t in DiscoveredToolRepository(kernel._storage).list(sym.id)}

        for op in parse_openapi(SPEC):
            tool = stored[op.tool_id]
            assert (tool.name, tool.description, tool.method) == (
                op.name, op.description, op.method
            )
            assert (tool.url_template, tool.parameters, tool.risk_level, tool.tags) == (
                op.url_template, op.parameters, op.risk_level, op.tags
            )
        assert len(stored) == 3
    finally:
        kernel.shutdown()


def test_the_package_exports_the_two_functions() -> None:
    assert set(discovery.__all__) == {"OpenAPIOperation", "http_config_for", "parse_openapi"}


# ── http_config_for ─────────────────────────────────────────────────────


def test_a_get_marks_the_optional_query_parameters() -> None:
    (search,) = [op for op in parse_openapi(SPEC) if op.tool_id == "search"]

    config = http_config_for(search.method, search.url_template, search.parameters)

    assert isinstance(config, HttpToolConfig)
    assert (config.method, config.url_template) == ("GET", search.url_template)
    assert (config.body_template, config.optional_params) == (None, ["limit"])


def test_a_body_gets_its_required_fields_and_the_lists_are_marked() -> None:
    (replace,) = [op for op in parse_openapi(SPEC) if op.tool_id == "replace_item"]

    config = http_config_for("put", replace.url_template, replace.parameters)

    assert config.method == "PUT"
    assert config.body_template == {"title": "{title}"}  # not the path or query ones
    assert config.optional_params == ["dry_run"]
    assert config.array_params == ["labels"]
    assert config.content_type == "application/json"


def test_nothing_is_trusted_unless_the_host_says_so() -> None:
    plain = http_config_for("GET", "https://api.example.com/x")
    assert plain.allow_internal is False
    assert (plain.headers, plain.header_factory, plain.timeout) == ({}, None, 30.0)

    def factory() -> dict[str, str]:
        return {"Authorization": "Bearer t"}

    mine = http_config_for(
        "GET", "http://127.0.0.1:9/x", {},
        headers={"X-Client": "host"}, header_factory=factory, allow_internal=True, timeout=5.0,
    )
    assert (mine.allow_internal, mine.timeout) == (True, 5.0)
    assert (mine.headers, mine.header_factory) == ({"X-Client": "host"}, factory)


def test_the_content_type_the_conversion_noted_is_kept() -> None:
    form = {"type": "object", "properties": {"file": {"type": "string"}},
            "_content_type": "multipart/form-data"}
    assert http_config_for("POST", "https://x.test/up", form).content_type == "multipart/form-data"


# ── a host that manages its own integrations ────────────────────────────


class _Api(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self) -> None:  # noqa: N802
        self.seen.append((self.server.server_address[1], self.path,
                          self.headers.get("Authorization")))
        data = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        pass


def test_two_integrations_with_the_same_operation_each_with_its_credential(
    tmp_path: Path,
) -> None:
    servers = [http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Api) for _ in range(2)]
    for srv in servers:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Api.seen = []
    ports = [srv.server_address[1] for srv in servers]
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    try:
        sym = kernel.create_symbiote(name="a", role="r")
        other = kernel.create_symbiote(name="b", role="r")
        # Both descriptions have an operation called "search".
        for name, port in zip(("crm", "wiki"), ports, strict=True):
            token = f"token-of-{name}"
            for op in parse_openapi({"paths": SPEC["paths"]}, f"http://127.0.0.1:{port}"):
                if op.method != "GET":
                    continue
                kernel.tool_gateway.register_http_tool(
                    ToolDescriptor(
                        tool_id=f"{name}__{op.tool_id}", name=op.name,
                        description=op.description, parameters=op.parameters,
                        risk_level=op.risk_level,
                    ),
                    http_config_for(
                        op.method, op.url_template, op.parameters,
                        header_factory=lambda token=token: {"Authorization": f"Bearer {token}"},
                        allow_internal=True,  # the test servers are on loopback
                    ),
                )

        # Registering authorizes nobody: a person turns each one on.
        refused = kernel.tool_gateway.execute(sym.id, None, "crm__search", {"q": "a"})
        assert refused.success is False and "blocked" in refused.error
        kernel.environment.add_tools(sym.id, ["crm__search", "wiki__search"])

        for tool in ("crm__search", "wiki__search"):
            result = kernel.tool_gateway.execute(sym.id, None, tool, {"q": "a b"})
            assert result.success, result.error

        assert _Api.seen == [
            (ports[0], "/search?q=a%20b", "Bearer token-of-crm"),
            (ports[1], "/search?q=a%20b", "Bearer token-of-wiki"),
        ]
        assert kernel.environment.list_tools(other.id) == []
        assert json.loads(json.dumps(kernel.environment.list_tools(sym.id))) == [
            "crm__search", "wiki__search",
        ]
    finally:
        kernel.shutdown()
        for srv in servers:
            srv.shutdown()
