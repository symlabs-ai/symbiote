"""Sequential (single-thread) interleaving: kernel._last_trace is a single slot."""
import logging
from common import *
logging.disable(logging.CRITICAL)

def fn(i, messages, config, tools):
    blob = messages[1]["content"]
    if "STUCK" in blob:
        if messages[-1]["content"].startswith("You are repeating"):
            return "desisto"
        return tc("echo", x=1)            # same call forever -> stagnation
    if "[Tool result: echo]" in messages[-1]["content"]:
        return "feito"
    return tc("echo", x=2)

def run(interleave):
    k = mk_kernel(FakeLLM(fn))
    sym = k.create_symbiote("a", "assistant")
    add_tool(k, sym.id, "echo", lambda p: {"ok": 1})
    k.environment.configure(sym.id, tools=["echo"])
    A = k.start_session(sym.id); B = k.start_session(sym.id)
    k.message(A.id, "STUCK please")           # A ends by stagnation
    if interleave:
        k.message(B.id, "normal")             # another session talks before A is closed
    k.close_session(A.id)
    score = k._storage.fetch_one("SELECT auto_score, stop_reason, total_tool_calls FROM session_scores WHERE session_id=?", (A.id,))
    mem = k._storage.fetch_all("SELECT content FROM memory_entries WHERE session_id=? AND type='procedural'", (A.id,))
    print(f"interleave={interleave}: score A={dict(score)} failure_memories={[m['content'][:50] for m in mem]}")
run(False); run(True)
