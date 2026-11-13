import os, tempfile, logging, threading, http.server, json
d = tempfile.mkdtemp(prefix="symbiote-repro-", dir=os.environ.get("PROBE_TMP")); os.chdir(d); logging.disable(logging.CRITICAL)
from symbiote.security.network import validate_url, SSRFError
from symbiote.environment.descriptors import HttpToolConfig
from symbiote.environment.tools import _make_http_handler
for u in ["http://127.0.0.1/", "http://[::ffff:127.0.0.1]/", "http://[::ffff:169.254.169.254]/latest/meta-data/", "http://[::]/"]:
    try: validate_url(u); print(f"validate_url {u:52} -> ACEITO")
    except SSRFError: print(f"validate_url {u:52} -> bloqueado")
seen = []
class Hd(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        seen.append(self.path); b = b'{"ok": true}'
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass
srv = http.server.HTTPServer(("127.0.0.1", 0), Hd); port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
h = _make_http_handler(HttpToolConfig(method="GET", url_template="{url}"))
for u in [f"http://127.0.0.1:{port}/interno", f"http://[::ffff:127.0.0.1]:{port}/interno"]:
    try: r = h({"url": u}); print("fetch", u.replace(str(port), "PORT"), "-> OK", str(r)[:60])
    except Exception as e: print("fetch", u.replace(str(port), "PORT"), "->", type(e).__name__)
h2 = _make_http_handler(HttpToolConfig(method="GET", url_template=f"http://127.0.0.1:{port}/api/items/{{id}}", allow_internal=True))
for v in ["42", "../../admin/users", "42?as_admin=1", "a b", "preço"]:
    seen.clear()
    try: h2({"id": v}); print(f"id={v!r:22} -> servidor recebeu {seen}")
    except Exception as e: print(f"id={v!r:22} -> {type(e).__name__}")
srv.shutdown()
