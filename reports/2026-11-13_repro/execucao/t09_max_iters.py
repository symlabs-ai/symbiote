"""Loop exhaustion returns success with empty/narration text; controller counts tool calls, not iterations."""
import logging, time
from common import *
logging.disable(logging.CRITICAL)

def run(label, fn, max_iters=10, **cfg):
    llm = FakeLLM(fn)
    k = mk_kernel(llm)
    sym = k.create_symbiote("a", "assistant")
    add_tool(k, sym.id, "fetch", cfg.pop("handler", lambda p: {"page": p.get("n")}))
    k.environment.configure(sym.id, tools=["fetch"], max_tool_iterations=max_iters, **cfg)
    s = k.start_session(sym.id)
    out = k.message(s.id, "pesquise")
    tr = k._capabilities.last_loop_trace
    print(f"{label}: text={out['text']!r} llm_calls={len(llm.calls)} iterations={tr.total_iterations} tool_calls={tr.total_tool_calls} stop={tr.stop_reason}")
    print("   persistido p/ o usuario:", repr(k._sessions.get_messages(s.id, limit=1)[0].content))

# (a) one call per iteration, model never stops -> budget exhausted
run("(a) 3 iteracoes, 1 call cada", lambda i, m, c, t: tc("fetch", n=i), max_iters=3)
# (b) same, with narration
run("(b) idem com narracao", lambda i, m, c, t: "Vou verificar mais uma fonte...\n" + tc("fetch", n=i), max_iters=3)
# (c) 4 independent calls in the FIRST response, max_tool_iterations=10 -> would need only 2 iterations
def par(i, m, c, t):
    if i == 0: return "\n".join(tc("fetch", n=j) for j in range(12))
    return "Resumo das 12 paginas."
run("(c) 12 calls paralelas na 1a iteracao, max_tool_iterations=10", par, max_iters=10)
# (d) loop_timeout
def slow(p): time.sleep(4); return {"ok": 1}
run("(d) loop_timeout=10s, tool 4s", lambda i, m, c, t: tc("fetch", n=i), max_iters=10, loop_timeout=10.0, handler=slow)
