import logging
from common import *
logging.disable(logging.CRITICAL)
def fn(i, messages, config, tools):
    blob = "\n".join(m["content"] for m in messages[2:])
    if "SECRET-4711" in blob: return "achei SECRET-4711"
    if i >= 3: return "nao consegui"
    return tc("get_tree", n=i)
llm = FakeLLM(fn)
k = mk_kernel(llm, tool_result_max_chars=100000)   # host raised the per-result ceiling, as KernelConfig docs suggest
sym = k.create_symbiote("a", "assistant")
add_tool(k, sym.id, "get_tree", lambda p: {"tree": "n" * 60000, "answer": "SECRET-4711"})
k.environment.configure(sym.id, tools=["get_tree"])
s = k.start_session(sym.id)
out = k.message(s.id, "leia a arvore")
print("resposta:", out["text"], "| tool execs:", len(out["tool_results"]),
      "| resultado visto pelo LLM:", any("SECRET-4711" in m["content"] for c in llm.calls for m in c["messages"]))
