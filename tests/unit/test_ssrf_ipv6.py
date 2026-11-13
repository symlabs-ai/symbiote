"""Regression: SSRF validation blocks IPv4-mapped IPv6 and every non-global
address (report item S-3)."""

from __future__ import annotations

import http.server
import threading

import pytest

from symbiote.environment.descriptors import HttpToolConfig
from symbiote.environment.tools import _make_http_handler
from symbiote.security.network import SSRFError, validate_url


@pytest.mark.parametrize(
    "url",
    [
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:7f00:1]/",
        "http://[::ffff:169.254.169.254]/latest/meta-data/",
        "http://[::ffff:10.0.0.5]/",
        "http://[::ffff:192.168.1.1]/",
        "http://[::]/",
        "http://0.0.0.0/",
        "http://[64:ff9b::7f00:1]/",  # NAT64 of 127.0.0.1
        "http://224.0.0.1/",  # multicast
        "http://240.0.0.1/",  # reserved
        "http://198.18.0.1/",  # benchmarking
    ],
)
def test_non_global_addresses_are_blocked(url: str) -> None:
    with pytest.raises(SSRFError):
        validate_url(url)


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1/", "http://169.254.169.254/", "http://[::1]/", "http://localhost/"],
)
def test_previously_blocked_still_blocked(url: str) -> None:
    with pytest.raises(SSRFError):
        validate_url(url)


def test_public_ipv6_passes() -> None:
    assert validate_url("http://[2606:4700:4700::1111]/") == "http://[2606:4700:4700::1111]/"


class _Redirect(http.server.BaseHTTPRequestHandler):
    target = ""
    hits: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        self.hits.append(self.path)
        if self.path == "/redir":
            self.send_response(302)
            self.send_header("Location", self.target)
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


def test_redirect_to_mapped_loopback_is_blocked(monkeypatch) -> None:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Redirect)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    _Redirect.hits = []
    _Redirect.target = f"http://[::ffff:127.0.0.1]:{port}/internal"
    # The first hop (the local test server) must pass validation, so it is
    # approved and pinned to 127.0.0.1 by a fake; every other URL goes
    # through the real check, so the redirect target is judged by its real
    # (mapped loopback) address. No external connection.
    from urllib.parse import urlsplit

    import symbiote.security.network as net

    real_validated = net.validated_addresses

    def fake_validated(url: str) -> list[str]:
        if urlsplit(url).hostname == "127.0.0.1":
            return ["127.0.0.1"]
        return real_validated(url)

    monkeypatch.setattr(net, "validated_addresses", fake_validated)
    handler = _make_http_handler(HttpToolConfig(url_template="{url}"))
    try:
        with pytest.raises(Exception):  # noqa: B017 — SSRFError or wrapped
            handler({"url": f"http://127.0.0.1:{port}/redir"})
        assert "/internal" not in _Redirect.hits
    finally:
        srv.shutdown()


def test_mapped_loopback_fetch_is_refused() -> None:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Redirect)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    _Redirect.hits = []
    handler = _make_http_handler(HttpToolConfig(url_template="{url}"))
    try:
        with pytest.raises(SSRFError):
            handler({"url": f"http://[::ffff:127.0.0.1]:{port}/secret"})
        assert _Redirect.hits == []
    finally:
        srv.shutdown()
