"""Compara fechar sessão pela rota HTTP vs. kernel.close_session (in-process, LLM falso, banco temporário)."""
import os, tempfile, logging
d = tempfile.mkdtemp(prefix="symbiote-repro-", dir=os.environ.get("PROBE_TMP")); os.chdir(d)
os.environ["SYMBIOTE_DB_PATH"] = os.path.join(d, "t.db"); os.environ["SYMBIOTE_SKILL_SCOPE"] = "per_symbiote"
from fastapi.testclient import TestClient
from symbiote.api import http as H
from symbiote.core.kernel import SymbioteKernel
from symbiote.adapters.llm.base import MockLLMAdapter
logging.disable(logging.CRITICAL)
H._kernel = SymbioteKernel(config=H._resolve_config(), llm=MockLLMAdapter(default_response="Entendido."))
c = TestClient(H.app)
H.get_adapter()
_, key = H._key_manager.create_key(tenant_id="t1", name="k", role="admin")
A = {"Authorization": f"Bearer {key}"}
st = H._kernel._storage
def counts(sess):
    q = lambda t, col="session_id": st.fetch_one(f"SELECT COUNT(*) c FROM {t} WHERE {col} = ?", (sess,))["c"]
    row = st.fetch_one("SELECT status, summary FROM sessions WHERE id = ?", (sess,))
    return dict(status=row["status"], summary=bool(row["summary"]), memories=q("memory_entries"), scores=q("session_scores"), reflection_audit=q("reflection_audit"), traces=q("execution_traces"))
sym = c.post("/symbiotes", headers=A, json={"name": "bot", "role": "assistant"}).json()["id"]
msgs = ["I prefer short and direct answers.", "Remember: I always use Python 3.12 and never use tabs.", "We decided to deploy every Friday."]
def run(close_via):
    sess = c.post("/sessions", json={"symbiote_id": sym, "goal": "teste"}).json()["id"]
    for m in msgs:
        r = c.post(f"/sessions/{sess}/chat", headers=A, json={"content": m}); assert r.status_code == 200, r.text
    if close_via == "http": assert c.post(f"/sessions/{sess}/close").status_code == 200
    else: H._kernel.close_session(sess)
    return counts(sess)
print("fechar via POST /sessions/{id}/close :", run("http"))
print("fechar via kernel.close_session()    :", run("kernel"))
print("SDK expõe close?", [m for m in dir(__import__('symbiote.sdk.client', fromlist=['SymbioteClient']).SymbioteClient) if 'close' in m or 'session' in m])
