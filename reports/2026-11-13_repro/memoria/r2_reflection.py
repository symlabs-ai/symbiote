from common import *
import json
def cnt(k, sql, *p): return k._storage.fetch_one(sql, p)["c"]

print("== A) reflection_mode=llm: importance fora de [0,1] derruba close_session ==")
k, d = fresh("r2a", MockLLMAdapter("ok"))
s = k.create_symbiote("s", "assistant")
evolver = MockLLMAdapter(json.dumps([
    {"action": "create", "type": "preference", "content": "User prefers dark mode", "importance": 0.6, "tags": ["ui"]},
    {"action": "create", "type": "factual", "content": "Deploys happen on Tuesdays", "importance": 1.5, "tags": ["deploy"]},
]))
k.set_evolver_llm(evolver)
k.environment.configure(symbiote_id=s.id, reflection_mode="llm")
sess = k.start_session(s.id)
k.message(sess.id, "I like dark mode; we deploy on Tuesdays")
for attempt in (1, 2):
    try:
        k.close_session(sess.id); print("close ok")
    except Exception as e:
        print(f"tentativa {attempt}: close_session levantou {type(e).__name__}: {str(e).splitlines()[0]}")
    print("   status sessão:", k._storage.fetch_one("select status from sessions where id=?", (sess.id,))["status"],
          "| memórias:", cnt(k, "select count(*) c from memory_entries"),
          "| session_scores:", cnt(k, "select count(*) c from session_scores where session_id=?", sess.id),
          "| reflection_audit:", cnt(k, "select count(*) c from reflection_audit"))
for bad in ('"high"', 'null', '-0.2'):
    evolver.default_response = '[{"type":"factual","content":"x is y in prod","importance":%s,"tags":["a"]}]' % bad
    try: k.close_session(sess.id); print(f"importance={bad}: ok")
    except Exception as e: print(f"importance={bad}: {type(e).__name__}")

print("\n== B) modo keyword (default): fechar sessão reaberta duplica memórias e scores ==")
k, d = fresh("r2b", MockLLMAdapter("Sure. As a rule I never store secrets in plain text, and I always double-check."))
s = k.create_symbiote("s", "assistant")
sess = k.get_or_create_session(s.id, external_key="user-1:chat")
k.message(sess.id, "hello there, how are you today?")
k.close_session(sess.id)
print("após 1o close:", [(r["type"], r["importance"], r["content"][:45]) for r in k._storage.fetch_all("select * from memory_entries")])
for i in range(3):
    sess2 = k.get_or_create_session(s.id, external_key="user-1:chat")   # reabre a mesma sessão
    assert sess2.id == sess.id
    k.message(sess.id, "ok thanks")
    k.close_session(sess.id)
print("após 4 closes: memórias =", cnt(k, "select count(*) c from memory_entries"),
      "| distintas =", cnt(k, "select count(distinct content) c from memory_entries"),
      "| session_scores da sessão =", cnt(k, "select count(*) c from session_scores where session_id=?", sess.id))
print("kernel.get_session() reabre sessão fechada:", k.get_session(sess.id).status,
      "->", k._storage.fetch_one("select status from sessions where id=?", (sess.id,))["status"])
