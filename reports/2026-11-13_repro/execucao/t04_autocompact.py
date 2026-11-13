"""Autocompact (layer 3) deletes the tool result the model has not seen yet."""
import logging
from common import *
logging.disable(logging.CRITICAL)

def fn(i, messages, config, tools):
    blob = "\n".join(m["content"] for m in messages[2:])
    if "SECRET-4711" in blob:
        return "O saldo e SECRET-4711"
    if i >= 6:
        return "nao consegui obter o saldo"
    return tc("get_balance", account=f"acc-{i}")   # vary params so stagnation does not trigger

def run(persona_chars, context_budget=4000):
    llm = FakeLLM(fn)
    k = mk_kernel(llm, context_budget=context_budget)
    sym = k.create_symbiote("a", "assistant", persona={"instructions": "x" * persona_chars})
    add_tool(k, sym.id, "get_balance", lambda p: {"balance": "SECRET-4711"})
    k.environment.configure(sym.id, tools=["get_balance"])
    s = k.start_session(sym.id)
    out = k.message(s.id, "qual o saldo?")
    text = out["text"] if isinstance(out, dict) else out
    sys_chars = len(llm.calls[0]["messages"][0]["content"])
    seen = any("SECRET-4711" in m["content"] for c in llm.calls for m in c["messages"])
    print(f"system_prompt={sys_chars} chars (~{sys_chars//4} tok) kernel.context_budget={context_budget} "
          f"llm_calls={len(llm.calls)} tool_execs={len(out['tool_results']) if isinstance(out, dict) else 0} "
          f"resultado_chegou_ao_LLM={seen} resposta={text!r}")
    if not seen:
        print("   2a chamada, msgs apos o system:", [(m['role'], m['content'][:160]) for m in llm.calls[1]['messages'][1:]])
run(1000)
run(52000)                      # ~13k tokens of system prompt (persona / tool schemas / extra_context)
run(52000, context_budget=200000)  # raising KernelConfig.context_budget does not help
