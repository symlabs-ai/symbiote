from common import *
import json
def cnt(k, sql, *p): return k._storage.fetch_one(sql, p)["c"]
k, d = fresh("r2a", MockLLMAdapter("ok"))
s = k.create_symbiote("s", "assistant")
evolver = MockLLMAdapter(json.dumps([
    {"action": "create", "type": "preference", "content": "User prefers dark mode", "importance": 0.6, "tags": ["ui"]},
    {"action": "create", "type": "factual", "content": "Deploys happen on Tuesdays", "importance": "high", "tags": ["deploy"]},
]))
k.set_evolver_llm(evolver)
k.environment.configure(symbiote_id=s.id, reflection_mode="llm")
sess = k.start_session(s.id)
k.message(sess.id, "I like dark mode; we deploy on Tuesdays")
for attempt in (1, 2, 3):
    try:
        k.close_session(sess.id); print("close ok")
    except Exception as e:
        print(f"tentativa {attempt}: close_session levantou {type(e).__name__}: {str(e).splitlines()[0]}")
    print("   status:", k._storage.fetch_one("select status from sessions where id=?", (sess.id,))["status"],
          "| memórias:", cnt(k, "select count(*) c from memory_entries"),
          "| session_scores:", cnt(k, "select count(*) c from session_scores where session_id=?", sess.id),
          "| reflection_audit:", cnt(k, "select count(*) c from reflection_audit"))
