"""Known #1: cross-session trace contamination, no monkeypatching, real threads."""
import json, threading, logging
from common import *
logging.disable(logging.CRITICAL)

def fn(i, messages, config, tools):
    user = messages[-1]["content"]
    # tool sessions: first LLM call asks the tool with the session marker; after tool result -> final text
    if "[Tool result: echo]" in user:
        return "feito"
    if "USE_TOOL" in messages[1]["content"] if len(messages) > 1 else False:
        marker = messages[1]["content"].split("USE_TOOL ")[1].split()[0]
        return tc("echo", marker=marker)
    return "ola"

llm = FakeLLM(fn)
llm.calls = type("L", (list,), {"append": lambda self, x: None})()  # don't accumulate
k = mk_kernel(llm)
sym = k.create_symbiote("a", "assistant")
add_tool(k, sym.id, "echo", lambda p: {"ok": p.get("marker")})
k.environment.configure(sym.id, tools=["echo"])

N_THREADS, ROUNDS = 8, 40
sessions = [k.start_session(sym.id) for _ in range(N_THREADS)]
tool_sessions = {s.id for i, s in enumerate(sessions) if i % 2 == 0}
errors = []
def worker(idx):
    s = sessions[idx]
    for r in range(ROUNDS):
        try:
            if s.id in tool_sessions:
                k.message(s.id, f"USE_TOOL {s.id} round{r}")
            else:
                k.message(s.id, f"oi round{r}")
        except Exception as e:
            errors.append(repr(e)[:200])
ts = [threading.Thread(target=worker, args=(i,)) for i in range(N_THREADS)]
[t.start() for t in ts]; [t.join() for t in ts]

rows = k._storage.fetch_all("SELECT session_id, steps_json FROM execution_traces")
foreign = 0; in_plain = 0; per_sess = {}
for r in rows:
    per_sess[r["session_id"]] = per_sess.get(r["session_id"], 0) + 1
    steps = json.loads(r["steps_json"])
    markers = {st["params"].get("marker") for st in steps}
    if r["session_id"] not in tool_sessions:
        in_plain += 1
    if markers - {r["session_id"]}:
        foreign += 1
expected = len(tool_sessions) * ROUNDS
print("errors:", len(errors), errors[:2])
print(f"traces esperados (so sessoes com tool): {expected}; linhas persistidas: {len(rows)}")
print(f"traces gravados em sessoes SEM tool call: {in_plain}")
print(f"traces cujo step.params.marker pertence a OUTRA sessao: {foreign}")
lost = sum(max(0, ROUNDS - per_sess.get(sid, 0)) for sid in tool_sessions)
dup = sum(max(0, per_sess.get(sid, 0) - ROUNDS) for sid in tool_sessions)
print(f"traces perdidos em sessoes com tool: {lost}; excedentes: {dup}")
