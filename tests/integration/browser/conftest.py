"""Fixtures for browser integration tests — local HTTP server, no internet."""

from __future__ import annotations

import http.server
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

_PAGES: dict[str, str] = {
    "/": """<!doctype html><html><head><title>Index</title></head>
<body>
<h1>Test site</h1>
<a href="/target">Go to target</a>
<form action="/submitted" method="get">
  <label>Search: <input name="q" type="text"></label>
  <button type="submit">Search</button>
</form>
</body></html>""",
    "/target": """<!doctype html><html><body>
<h1>Arrived</h1>
<p id="msg">You made it.</p>
</body></html>""",
    "/submitted": """<!doctype html><html><body>
<h1>Submitted</h1>
</body></html>""",
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        body = _PAGES.get(path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body.encode())))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args, **kwargs):  # silence
        return


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def local_site() -> Iterator[str]:
    port = _free_port()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
