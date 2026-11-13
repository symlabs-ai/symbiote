"""HTTP tool / SSRF probes. Local servers only (127.0.0.1, ephemeral ports)."""
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from symbiote.environment.descriptors import HttpToolConfig
from symbiote.environment.tools import _make_http_handler
from symbiote.security.network import SSRFError, validate_url

LOG = []


class H(BaseHTTPRequestHandler):
    redirect_to = None

    def _go(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        LOG.append({
            "srv": self.server.server_address[1], "method": self.command,
            "path": self.path, "auth": self.headers.get("Authorization"),
            "body": body,
        })
        if self.path.startswith("/redir") and self.server.redirect_to:
            self.send_response(302)
            self.send_header("Location", self.server.redirect_to)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    do_GET = do_POST = do_DELETE = do_PUT = _go

    def log_message(self, *a):
        pass


def serve():
    s = ThreadingHTTPServer(("127.0.0.1", 0), H)
    s.redirect_to = None
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


A, B = serve(), serve()
pa, pb = A.server_address[1], B.server_address[1]


def call(cfg, params):
    LOG.clear()
    try:
        out = _make_http_handler(cfg)(params)
        return "OK", out, list(LOG)
    except Exception as e:  # noqa: BLE001
        return "ERR", f"{type(e).__name__}: {e}", list(LOG)


print("== 1. validate_url on loopback spellings ==")
for u in [
    f"http://127.0.0.1:{pa}/", f"http://localhost:{pa}/", f"http://2130706433:{pa}/",
    f"http://127.1:{pa}/", f"http://[::1]:{pa}/",
    f"http://[::ffff:127.0.0.1]:{pa}/", f"http://[::ffff:7f00:1]:{pa}/",
    "http://[::ffff:169.254.169.254]/latest/meta-data/",
    "http://[::ffff:10.0.0.5]/", "http://[::]/", "http://LOCALHOST./",
    "file:///etc/passwd", "ftp://127.0.0.1/x", "gopher://127.0.0.1/x",
]:
    try:
        validate_url(u)
        r = "ALLOWED"
    except SSRFError as e:
        r = f"blocked (SSRFError)"
    except ValueError as e:
        r = f"rejected (ValueError: {str(e)[:50]})"
    print(f"  {u:55s} -> {r}")

print("\n== 2. end-to-end: allow_internal=False tool, LLM-controlled URL reaches loopback ==")
cfg = HttpToolConfig(method="GET", url_template="{url}")
print("  plain 127.0.0.1 :", call(cfg, {"url": f"http://127.0.0.1:{pa}/secret"})[:2])
st, out, log = call(cfg, {"url": f"http://[::ffff:127.0.0.1]:{pa}/secret"})
print("  ::ffff:127.0.0.1:", st, out, "| server saw:", [(e["srv"] == pa, e["path"]) for e in log])

print("\n== 3. redirect handling (first hop via the mapped-IPv6 bypass, allow_internal=False) ==")
A.redirect_to = f"http://127.0.0.1:{pb}/internal"
st, out, log = call(cfg, {"url": f"http://[::ffff:127.0.0.1]:{pa}/redir"})
print("  302 -> http://127.0.0.1      :", st, str(out)[:90], "| hops:", [e["path"] for e in log])
A.redirect_to = f"http://[::ffff:127.0.0.1]:{pb}/internal"
st, out, log = call(cfg, {"url": f"http://[::ffff:127.0.0.1]:{pa}/redir"})
print("  302 -> http://[::ffff:127...]:", st, str(out)[:60], "| hops:", [e["path"] for e in log])

print("\n== 4. auth header forwarded across origins on redirect ==")
A.redirect_to = f"http://127.0.0.1:{pb}/collect"
cfg = HttpToolConfig(method="GET", url_template=f"http://127.0.0.1:{pa}/redir", allow_internal=True,
                     header_factory=lambda: {"Authorization": "Bearer HOST-SECRET"})
st, out, log = call(cfg, {})
for e in log:
    print(f"  server:{'A' if e['srv'] == pa else 'B'} path={e['path']} Authorization={e['auth']}")

print("\n== 5. url_template substitution is not URL-encoded ==")
cfg = HttpToolConfig(method="GET", url_template=f"http://127.0.0.1:{pa}/api/items/{{id}}", allow_internal=True,
                     header_factory=lambda: {"Authorization": "Bearer HOST-SECRET"})
for v in ["42", "../../admin/users", "42?force=true&as_admin=1", "42#x", "a b", "café", "a%2Fb"]:
    st, out, log = call(cfg, {"id": v})
    print(f"  id={v!r:28} -> {st} {'' if st == 'OK' else out[:75]} server path: {[e['path'] for e in log]}")
print("  README example  search?q={q}:")
cfg = HttpToolConfig(method="GET", url_template=f"http://127.0.0.1:{pa}/api/search?q={{q}}", allow_internal=True)
for v in ["dataclasses", "python dataclasses", "preço", "a&limit=9999"]:
    st, out, log = call(cfg, {"q": v})
    print(f"  q={v!r:22} -> {st} {'' if st == 'OK' else out[:80]} server path: {[e['path'] for e in log]}")

print("\n== 6. body: undeclared params are merged into the request body ==")
cfg = HttpToolConfig(method="POST", url_template=f"http://127.0.0.1:{pa}/api/users/{{id}}", allow_internal=True,
                     body_template={"display_name": "{display_name}"})
st, out, log = call(cfg, {"id": "7", "display_name": "x", "role": "admin", "is_superuser": True})
print("  ", st, "body sent:", log[0]["body"] if log else out)
cfg = HttpToolConfig(method="POST", url_template=f"http://127.0.0.1:{pa}/api/users/{{id}}", allow_internal=True)
st, out, log = call(cfg, {"id": "7", "display_name": "x"})
print("   no body_template -> body:", log[0]["body"] if log else out)
cfg = HttpToolConfig(method="POST", url_template=f"http://127.0.0.1:{pa}/api/x", allow_internal=True,
                     body_template={"q": '{{"term": "{t}"}}', "n": "{n}"})
print("   template w/ partial placeholder + int:", call(cfg, {"t": "a", "n": 5})[2][0]["body"])

print("\n== 7. allow_internal via dict / model_validate ==")
c = HttpToolConfig.model_validate({"url_template": "http://x/", "allow_internal": True})
print("  model_validate({... allow_internal: True}).allow_internal =", c.allow_internal)
c = HttpToolConfig(**json.loads('{"url_template": "http://x/", "allow_internal": true}'))
print("  HttpToolConfig(**json).allow_internal =", c.allow_internal, "| model_dump has key:", "allow_internal" in c.model_dump())

print("\n== 8. response handling ==")
import http.server


class Big(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        if self.path == "/bin":
            self.wfile.write(b"\xff\xfe\x00binary")
        else:
            chunk = b"A" * 65536
            for _ in range(800):  # ~52 MB
                self.wfile.write(chunk)

    def log_message(self, *a):
        pass


C = ThreadingHTTPServer(("127.0.0.1", 0), Big)
threading.Thread(target=C.serve_forever, daemon=True).start()
pc = C.server_address[1]
cfg = HttpToolConfig(method="GET", url_template=f"http://127.0.0.1:{pc}/{{p}}", allow_internal=True)
st, out, _ = call(cfg, {"p": "bin"})
print("  non-utf8 body ->", st, str(out)[:80])
st, out, _ = call(cfg, {"p": "big"})
print("  52MB body ->", st, "returned chars:", len(out) if isinstance(out, str) else out)

for s in (A, B, C):
    s.shutdown()
    s.server_close()
