import os, tempfile, logging, threading, collections
d = tempfile.mkdtemp(prefix="symbiote-repro-", dir=os.environ.get("PROBE_TMP")); os.chdir(d)
logging.disable(logging.CRITICAL)
from symbiote.core.kernel import SymbioteKernel
from symbiote.config.models import KernelConfig
from symbiote.core.models import MemoryEntry
from pathlib import Path
class SpyLLM:
    def __init__(self): self.prompts = []
    def complete(self, messages, config=None, tools=None, **kw):
        self.prompts.append("\n".join(str(m.get("content")) for m in messages)); return "ok"
    def stream(self, messages, config=None, tools=None, **kw):
        self.complete(messages); yield "ok"
llm = SpyLLM()
k = SymbioteKernel(KernelConfig(db_path=Path(d) / "t.db"), llm=llm)
a = k.create_symbiote(name="A", role="assistant"); b = k.create_symbiote(name="B", role="assistant")
k._memory.store(MemoryEntry(symbiote_id=a.id, type="factual", scope="global", source="user", content="O token do cliente Acme é ACME-9931", importance=0.9))
k._memory.store(MemoryEntry(symbiote_id=b.id, type="preference", scope="global", source="user", content="User prefers the vim editor", importance=0.9))
sb = k.start_session(b.id)
def ask(q):
    n = len(llm.prompts); k.message(sb.id, q); p = "\n".join(llm.prompts[n:]); return ("ACME-9931" in p, "vim editor" in p)
print("chamadas ao LLM registradas:", "sim" if (ask("oi") and llm.prompts) else "NÃO (spy não funcionou)")
for q in ["token do cliente", "%", "Which editor do I prefer?", "vim"]:
    leak, own = ask(q)
    print(f"B pergunta {q!r:28} -> segredo do A no prompt de B: {leak} | memória própria de B no prompt: {own}")
print("MemoryStore.search('Acme') sem filtro:", [e.symbiote_id == a.id for e in k._memory.search(query="Acme")])
# corrida external_key
res = []
for r in range(20):
    key = f"k{r}"; ids = []; bar = threading.Barrier(8)
    def w():
        bar.wait(); ids.append(k.get_or_create_session(symbiote_id=a.id, external_key=key).id)
    ts = [threading.Thread(target=w) for _ in range(8)]; [t.start() for t in ts]; [t.join() for t in ts]
    res.append(len(set(ids)))
print("get_or_create_session, 8 threads, mesma chave nova — sessões criadas por rodada:", dict(collections.Counter(res)))
