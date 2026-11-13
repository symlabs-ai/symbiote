from common import *
import json, time, threading
from symbiote.core.models import MemoryEntry
import logging; logging.disable(logging.CRITICAL)
def cnt(k, sql, *p): return k._storage.fetch_one(sql, p)["c"]
def mem(k, sid, content, type="constraint", tags=(), imp=0.8):
    return k._memory.store(MemoryEntry(symbiote_id=sid, type=type, scope="global", source="reflection",
                                       content=content, tags=list(tags), importance=imp))

print("== A) ReconcilePhase (light, sem LLM) desativa fatos distintos que só compartilham tags ==")
k, d = fresh("r4a")
s = k.create_symbiote("s", "assistant")
facts = ["Never deploy to production on Fridays without written approval",
         "Always run the linter before committing Python code",
         "Database passwords must rotate every ninety days",
         "Customer emails are answered within one business day"]
for f in facts: mem(k, s.id, f, tags=["constraint", "team-policy"])
rep = k.dream(s.id)
print("fases:", [(p.phase, p.actions_applied) for p in rep.phases])
print("ativas depois:", [r["content"][:40] for r in k._storage.fetch_all("select content from memory_entries where is_active=1")])

print("\n== B) dry_run grava dream_report e zera a janela 'desde o último dream' ==")
k, d = fresh("r4b")
s = k.create_symbiote("s", "assistant")
k.environment.configure(symbiote_id=s.id, dream_mode="light", dream_min_sessions=3)
for i in range(3):
    x = k.start_session(s.id); k._sessions.close(x.id)
eng = k._get_or_create_dream_engine(k.environment.get_config(s.id), s.id)
print("should_dream antes do dry-run:", eng.should_dream(s.id, "light"))
k.dream(s.id, dry_run=True)
print("dream_reports:", k._storage.fetch_all("select dry_run, completed_at is not null as done from dream_reports"))
print("should_dream depois do dry-run:", eng.should_dream(s.id, "light"))

print("\n== C) GeneralizePhase regeneraliza o mesmo cluster a cada ciclo ==")
llm = MockLLMAdapter("Always validate inputs before calling the billing API.")
k, d = fresh("r4c", llm)
s = k.create_symbiote("s", "assistant")
k.environment.configure(symbiote_id=s.id, dream_mode="full")
for i in range(3):
    mem(k, s.id, f"Always validate inputs before calling the billing API endpoint number {i}", type="procedural", tags=["billing", "api"], imp=0.6)
for cycle in range(1, 4):
    k.dream(s.id)
    rows = k._storage.fetch_all("select importance, is_active from memory_entries where tags_json like '%dream:generalized%'")
    print(f"ciclo {cycle}: generalizadas={len(rows)} importances={[r['importance'] for r in rows]} | LLM calls={len(llm.calls)} | ativas total={cnt(k,'select count(*) c from memory_entries where is_active=1')}")

print("\n== D) scope global: DreamEngine singleton congela config do 1o symbiote ==")
k, d = fresh("r4d")
a = k.create_symbiote("a", "x"); b = k.create_symbiote("b", "x")
k.environment.configure(symbiote_id=a.id, dream_mode="light", dream_min_sessions=50, dream_max_llm_calls=2)
k.environment.configure(symbiote_id=b.id, dream_mode="light", dream_min_sessions=1, dream_max_llm_calls=40)
k._maybe_dream(a.id)
x = k.start_session(b.id); k.message(x.id, "hi"); k.close_session(x.id)
time.sleep(0.3)
e = k._dream_engine
print("engine usado para B: min_sessions=%d max_llm_calls=%d | dream_reports de B=%d (esperado 1, min_sessions de B = 1)" % (
    e._min_sessions, e._max_llm_calls, cnt(k, "select count(*) c from dream_reports where symbiote_id=?", b.id)))

print("\n== E) scope per_symbiote: engine novo a cada close -> guarda 'already running' nunca dispara ==")
class Slow:
    def __init__(self): self.n = 0
    def complete(self, messages, config=None, tools=None):
        self.n += 1; time.sleep(1.0); return "Always check the cache before calling the API."
slow = Slow()
k, d = fresh("r4e", MockLLMAdapter("ok"), skill_scope="per_symbiote")
k.set_evolver_llm(slow)
s = k.create_symbiote("s", "assistant")
k.environment.configure(symbiote_id=s.id, dream_mode="full", dream_min_sessions=1)
for i in range(3):
    mem(k, s.id, f"Always check the cache before calling the pricing API variant {i}", type="procedural", tags=["pricing", "api"], imp=0.6)
for i in range(4):
    x = k.start_session(s.id); k.message(x.id, "hi"); k.close_session(x.id)
alive = [t.name for t in threading.enumerate() if t.name.startswith("dream-")]
print("threads de dream simultâneas para o mesmo symbiote:", len(alive))
time.sleep(2.5)
print("LLM calls:", slow.n, "| dream_reports:", cnt(k, "select count(*) c from dream_reports"),
      "| memórias generalizadas duplicadas:", cnt(k, "select count(*) c from memory_entries where tags_json like '%dream:generalized%'"))
