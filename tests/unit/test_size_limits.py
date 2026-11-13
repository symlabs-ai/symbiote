"""Regression: request bodies, text fields and HTTP-tool responses are bounded
(report item S-10)."""

from __future__ import annotations

import http.server
import threading

import pytest

from symbiote.api import limits
from symbiote.environment.descriptors import HttpToolConfig
from symbiote.environment.tools import _make_http_handler


@pytest.fixture()
def owned(api_client):
    api = api_client()
    h = api.auth(api.new_key("tenant-a"))
    sym = api.client.post("/symbiotes", headers=h, json={"name": "A", "role": "r"}).json()
    sess = api.client.post("/sessions", headers=h, json={"symbiote_id": sym["id"]}).json()
    return api, h, sess["id"]


def test_message_over_limit_is_rejected(owned) -> None:
    api, h, sess = owned
    big = "x" * (limits.MAX_MESSAGE_CHARS + 1)
    resp = api.client.post(
        f"/sessions/{sess}/messages", headers=h, json={"role": "user", "content": big}
    )
    assert resp.status_code in (413, 422)


def test_message_at_limit_is_accepted(owned) -> None:
    api, h, sess = owned
    ok = "x" * limits.MAX_MESSAGE_CHARS
    resp = api.client.post(
        f"/sessions/{sess}/messages", headers=h, json={"role": "user", "content": ok}
    )
    assert resp.status_code == 201


def test_body_over_limit_is_413(owned) -> None:
    api, h, sess = owned
    payload = b'{"role": "user", "content": "' + b"x" * limits.MAX_BODY_BYTES + b'"}'
    resp = api.client.post(
        f"/sessions/{sess}/messages",
        headers={**h, "Content-Type": "application/json"},
        content=payload,
    )
    assert resp.status_code == 413


def test_chunked_body_over_limit_is_413(owned) -> None:
    api, h, sess = owned
    chunk = b"x" * (256 * 1024)
    count = limits.MAX_BODY_BYTES // len(chunk) + 2

    def gen():
        yield b'{"role": "user", "content": "'
        for _ in range(count):
            yield chunk
        yield b'"}'

    resp = api.client.post(
        f"/sessions/{sess}/messages",
        headers={**h, "Content-Type": "application/json"},
        content=gen(),
    )
    assert resp.status_code == 413


def test_symbiote_name_over_limit_is_rejected(owned) -> None:
    api, h, _sess = owned
    resp = api.client.post(
        "/symbiotes", headers=h, json={"name": "x" * 1_000_000, "role": "r"}
    )
    assert resp.status_code == 422


class _BigHandler(http.server.BaseHTTPRequestHandler):
    size = 0

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(self.size))
        self.end_headers()
        sent = 0
        block = b"y" * 65536
        try:
            while sent < self.size:
                n = min(len(block), self.size - sent)
                self.wfile.write(block[:n])
                sent += n
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args) -> None:
        pass


@pytest.fixture()
def big_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _BigHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def test_http_tool_response_over_cap_is_an_error(big_server) -> None:
    from symbiote.environment import tools as tools_mod

    _BigHandler.size = tools_mod.MAX_HTTP_TOOL_RESPONSE_BYTES + 1
    port = big_server.server_address[1]
    handler = _make_http_handler(
        HttpToolConfig(url_template=f"http://127.0.0.1:{port}/big", allow_internal=True)
    )
    with pytest.raises(RuntimeError, match="exceeds"):
        handler({})


def test_http_tool_response_under_cap_is_returned(big_server) -> None:
    _BigHandler.size = 1000
    port = big_server.server_address[1]
    handler = _make_http_handler(
        HttpToolConfig(url_template=f"http://127.0.0.1:{port}/ok", allow_internal=True)
    )
    out = handler({})
    assert "y" * 1000 in out
