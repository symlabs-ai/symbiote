"""Regression: values from the LLM cannot change the path or query of an HTTP
tool's URL, and ordinary values (spaces, accents, falsy) work (report item S-7)."""

from __future__ import annotations

import http.server
import threading

import pytest

from symbiote.environment.descriptors import HttpToolConfig
from symbiote.environment.tools import _build_url, _make_http_handler


class _Echo(http.server.BaseHTTPRequestHandler):
    seen: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        self.seen.append(self.path)
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture()
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Echo.seen = []
    yield srv.server_address[1]
    srv.shutdown()


def _call(port: int, template: str, params: dict, **cfg) -> str:
    handler = _make_http_handler(
        HttpToolConfig(
            url_template=f"http://127.0.0.1:{port}{template}", allow_internal=True, **cfg
        )
    )
    _Echo.seen = []
    handler(params)
    return _Echo.seen[-1]


@pytest.mark.parametrize(
    ("value", "expected_path"),
    [
        ("42", "/api/items/42"),
        ("../../admin/users", "/api/items/..%2F..%2Fadmin%2Fusers"),
        ("42?as_admin=1", "/api/items/42%3Fas_admin%3D1"),
        ("a b", "/api/items/a%20b"),
        ("preço", "/api/items/pre%C3%A7o"),
    ],
)
def test_path_placeholder_is_a_single_encoded_segment(server, value, expected_path) -> None:
    assert _call(server, "/api/items/{id}", {"id": value}) == expected_path


def test_query_values_are_encoded(server) -> None:
    path = _call(server, "/api/search?q={q}", {"q": "python dataclasses & more"})
    assert path == "/api/search?q=python%20dataclasses%20%26%20more"


def test_injected_query_param_stays_inside_value(server) -> None:
    path = _call(server, "/api/search?q={q}", {"q": "a&limit=9999"})
    assert path == "/api/search?q=a%26limit%3D9999"


def test_falsy_optional_values_are_kept() -> None:
    url = _build_url(
        "http://h/items?archived={archived}&offset={offset}&tag={tag}",
        {"archived": False, "offset": 0, "tag": None},
        optional_params=["archived", "offset", "tag"],
    )
    assert url == "http://h/items?archived=False&offset=0"


def test_empty_string_optional_is_dropped() -> None:
    url = _build_url(
        "http://h/items?status={status}", {"status": ""}, optional_params=["status"]
    )
    assert url == "http://h/items"


def test_leading_placeholder_is_not_encoded() -> None:
    # Templates that start with a placeholder (``{base_url}/...`` or a whole
    # ``{url}``) receive a URL there; only the following placeholders are encoded.
    assert _build_url("{base_url}/items/{id}", {"base_url": "http://h:1", "id": "a/b"}, []) == (
        "http://h:1/items/a%2Fb"
    )
    assert _build_url("{url}", {"url": "http://h/x?y=1"}, []) == "http://h/x?y=1"
