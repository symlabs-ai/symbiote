"""Regression: an HTTP tool never forwards its credentials to another origin
on redirect (report item S-8)."""

from __future__ import annotations

import http.server
import threading

import pytest

from symbiote.environment.descriptors import HttpToolConfig
from symbiote.environment.tools import _make_http_handler


def _server(name: str, log: list):
    class Handler(http.server.BaseHTTPRequestHandler):
        redirect_to = ""

        def do_GET(self) -> None:  # noqa: N802
            log.append(
                (
                    name,
                    self.path,
                    self.headers.get("Authorization"),
                    self.headers.get("X-Api-Key"),
                    self.headers.get("Cookie"),
                )
            )
            if self.path.startswith("/redir") and Handler.redirect_to:
                self.send_response(302)
                self.send_header("Location", Handler.redirect_to)
                self.end_headers()
                return
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, Handler


@pytest.fixture()
def servers():
    log: list = []
    a, ha = _server("A", log)
    b, hb = _server("B", log)
    yield log, a.server_address[1], ha, b.server_address[1]
    a.shutdown()
    b.shutdown()


def _handler(port_a: int):
    return _make_http_handler(
        HttpToolConfig(
            url_template=f"http://127.0.0.1:{port_a}/redir",
            allow_internal=True,
            headers={"X-Api-Key": "STATIC-SECRET", "Cookie": "sid=1"},
            header_factory=lambda: {"Authorization": "Bearer HOST-SECRET"},
        )
    )


def test_cross_origin_redirect_drops_credentials(servers) -> None:
    log, port_a, handler_a, port_b = servers
    handler_a.redirect_to = f"http://127.0.0.1:{port_b}/collect"

    _handler(port_a)({})

    first, second = log
    assert first[0] == "A" and first[2] == "Bearer HOST-SECRET"
    assert second[0] == "B"
    assert second[2:] == (None, None, None)


def test_same_origin_redirect_keeps_credentials(servers) -> None:
    log, port_a, handler_a, _port_b = servers
    handler_a.redirect_to = f"http://127.0.0.1:{port_a}/final"

    _handler(port_a)({})

    assert [entry[0] for entry in log] == ["A", "A"]
    assert log[1][2] == "Bearer HOST-SECRET"
    assert log[1][3] == "STATIC-SECRET"
