"""SymbioteClient vs the real FastAPI app, in-process (starlette TestClient, no sockets)."""
import logging
import os
import sys
from pathlib import Path

HERE = Path(sys.argv[1])
for f in HERE.glob("api.db*"):
    f.unlink()
os.environ["SYMBIOTE_DB_PATH"] = str(HERE / "api.db")
os.environ["SYMBIOTE_LOCAL_ADMIN"] = "1"
os.environ["SYMBIOTE_SKILL_SCOPE"] = "per_symbiote"
os.environ.pop("SYMBIOTE_LLM_PROVIDER", None)

from fastapi.testclient import TestClient

import symbiote.api.http as api
from symbiote.sdk import SymbioteClient

logging.disable(logging.CRITICAL)
tc = TestClient(api.app)

r = tc.post("/symbiotes", json={"name": "Atlas", "role": "assistant"})
print("README:88-90 curl POST /symbiotes without Authorization ->", r.status_code, r.text[:80])
r0 = tc.post("/symbiotes", json={"name": "Atlas", "role": "assistant"}); print("  same call repeated ->", r0.status_code, r0.text[:60])
api.get_adapter()
r0 = tc.post("/symbiotes", json={"name": "Atlas", "role": "assistant"}); print("  after adapter init, still no Authorization ->", r0.status_code, r0.text[:60])
key = api._local_admin_key
print("local admin key provisioned:", bool(key))

client = SymbioteClient("http://testserver", api_key=key)
client._client.close()
client._client = TestClient(api.app, headers={"Authorization": f"Bearer {key}"})


def step(label, fn):
    try:
        out = fn()
        print(f"[ OK ] {label} -> {str(out)[:150]}")
        return out
    except Exception as e:  # noqa: BLE001
        body = getattr(getattr(e, "response", None), "text", "")
        print(f"[FAIL] {label} -> {type(e).__name__}: {str(e).splitlines()[0][:90]} {body[:160]}")
        return None


step("health()", client.health)
sym = step("create_symbiote(persona=...)", lambda: client.create_symbiote("Assistant", "helper", persona={"tone": "x"}))
step("get_symbiote", lambda: client.get_symbiote(sym["id"]))
sess = step("create_session(goal, external_key)", lambda: client.create_session(sym["id"], goal="g", external_key="u:1"))
step("get_session", lambda: client.get_session(sess["id"]))
step("chat (docstring example, server default = no LLM)", lambda: client.chat(sess["id"], "Hello!"))
step("chat(generation_settings={'temperature': 0})", lambda: client.chat(sess["id"], "Hello!", generation_settings={"temperature": 0}))
step("search_memory", lambda: client.search_memory("Hello", scope="global", limit=5))
step("register_tool", lambda: client.register_tool(sym["id"], "t1", "T1", "desc", "https://example.invalid/x/{id}", method="GET",
                                                    parameters={"type": "object", "properties": {"id": {"type": "string"}}}))
step("list_tools", lambda: client.list_tools(sym["id"]))
step("close_session", lambda: client.close_session(sess["id"]))
print("public SDK methods:", [m for m in dir(SymbioteClient) if not m.startswith("_")])
