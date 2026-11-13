"""SubagentManager._depth is one counter per kernel (not per call chain); failed spawns leak sessions."""
import logging, time, threading
from common import *
logging.disable(logging.CRITICAL)

def fn(i, messages, config, tools):
    user = messages[-1]["content"]; first = messages[1]["content"]
    if "SUBTASK" in first:
        if "FAIL" in first: raise RuntimeError("401 unauthorized")
        time.sleep(0.4); return "sub-resultado"
    if "[Tool result: spawn]" in user or "[Tool error: spawn]" in user:
        return "ok"
    if "FANOUT" in first:
        return "\n".join(tc("spawn", target_symbiote="worker", task=f"SUBTASK {j}") for j in range(5))
    if "DELEGATE_FAIL" in first:
        return tc("spawn", target_symbiote="worker", task="SUBTASK FAIL")
    return tc("spawn", target_symbiote="worker", task="SUBTASK unico")

k = mk_kernel(FakeLLM(fn))
boss = k.create_symbiote("boss", "assistant"); worker = k.create_symbiote("worker", "assistant")
k.environment.configure(boss.id, tools=["spawn"])

def spawn_outcomes(out):
    return [("OK" if r["output"]["success"] else r["output"]["error"]) for r in out["tool_results"]]

# (a) 5 independent delegations in ONE response (nesting depth is 1)
s = k.start_session(boss.id)
print("(a) 5 spawns paralelos na mesma resposta:", spawn_outcomes(k.message(s.id, "FANOUT")))

# (b) 6 different user sessions delegating at the same time (nesting depth is 1 for all)
res = {}
def w(i):
    ss = k.start_session(boss.id); res[i] = spawn_outcomes(k.message(ss.id, "delegue"))
ts = [threading.Thread(target=w, args=(i,)) for i in range(6)]; [t.start() for t in ts]; [t.join() for t in ts]
print("(b) 6 sessoes simultaneas, 1 spawn cada:", [r[0][:40] for r in res.values()])

# (c) failed sub-run: sub-session is never closed
before = k._storage.fetch_one("SELECT COUNT(*) c FROM sessions WHERE status='active' AND goal LIKE '[subagent]%'")["c"]
s = k.start_session(boss.id); out = k.message(s.id, "DELEGATE_FAIL")
after = k._storage.fetch_all("SELECT status, goal FROM sessions WHERE goal LIKE '[subagent] SUBTASK FAIL%'")
print("(c) spawn com falha:", spawn_outcomes(out), "| sub-sessao:", [dict(r) for r in after], "| depth final:", k._subagent._depth)
