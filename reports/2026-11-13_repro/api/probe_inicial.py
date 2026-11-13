import os, sys, time, tempfile
d = tempfile.mkdtemp(prefix="symbiote-repro-", dir=os.environ.get("PROBE_TMP"))
os.environ["SYMBIOTE_DB_PATH"] = os.path.join(d, "t.db")
os.environ.pop("SYMBIOTE_DEV_MODE", None); os.environ.pop("SYMBIOTE_LOCAL_ADMIN", None)
os.chdir(d)
import logging
from fastapi.testclient import TestClient
from symbiote.api import http as H
logging.disable(logging.CRITICAL)
c = TestClient(H.app)

print("1. POST /symbiotes sem auth ->", c.post("/symbiotes", json={"name": "a", "role": "b"}).status_code)
# cria symbiote direto no storage (simula tenant existente)
from symbiote.core.identity import IdentityManager
sym = IdentityManager(storage=H.get_adapter()).create(name="Victim", role="assistant", owner_id="tenant-A") if "owner_id" in IdentityManager.create.__code__.co_varnames else IdentityManager(storage=H.get_adapter()).create(name="Victim", role="assistant")
r = c.post("/sessions", json={"symbiote_id": sym.id, "goal": "x"}); print("2. POST /sessions sem auth ->", r.status_code)
sid = r.json()["id"]
print("3. POST /sessions/{id}/messages sem auth ->", c.post(f"/sessions/{sid}/messages", json={"role": "assistant", "content": "injetado"}).status_code)
print("4. GET /api/sessions/{id}/messages sem auth ->", c.get(f"/api/sessions/{sid}/messages").status_code, c.get(f"/api/sessions/{sid}/messages").json()[:1])
for p in ("/api/dashboard", "/api/logs", "/api/symbiotes", f"/api/symbiotes/{sym.id}/memory", f"/api/symbiotes/{sym.id}/activity", "/memory/search?query=a"):
    print("5. GET", p.replace(sym.id, "{id}"), "sem auth ->", c.get(p).status_code)
r = c.post(f"/symbiotes/{sym.id}/tools", json={"tool_id": "probe", "name": "p", "description": "d", "parameters": {"type": "object", "properties": {}}, "http_method": "GET", "url_template": "http://example.invalid/x"})
print("6. POST /symbiotes/{id}/tools sem auth ->", r.status_code)
r = c.post(f"/symbiotes/{sym.id}/tools/probe/exec", json={"params": {}})
print("7. POST .../tools/probe/exec sem auth ->", r.status_code, (r.json().get("error") or "")[:70])
print("8. POST /sessions/{id}/close sem auth ->", c.post(f"/sessions/{sid}/close").status_code)
k = H.get_kernel()
print("9. tool 'probe' visivel no gateway do kernel (usado pelo /chat)?", k.tool_gateway.has_tool("probe"), "| no gateway da API?", H._tool_gateway.has_tool("probe"))

# timeout do PolicyGate
from symbiote.environment.policies import PolicyGate
class Env:
    def is_tool_enabled(self, *a, **k): return True
class St:
    def execute(self, *a, **k): pass
g = PolicyGate(Env(), St())
t = time.monotonic(); res = g.execute_with_policy("s", None, "t", {}, lambda p: time.sleep(2), timeout=0.2)
print(f"10. timeout=0.2s, tool dorme 2s -> retornou em {time.monotonic()-t:.2f}s; error={res.error}")

# allowed_root
from symbiote.environment.tools import _validate_path
root = os.path.join(d, "ws"); evil = os.path.join(d, "ws-evil"); os.makedirs(root); os.makedirs(evil)
try:
    print("11. path fora do root com prefixo igual aceito ->", _validate_path({"path": os.path.join(evil, "f"), "allowed_root": root}))
except PermissionError as e: print("11. bloqueado:", e)
print("12. sem allowed_root nos params ->", _validate_path({"path": "/etc/passwd"}))
