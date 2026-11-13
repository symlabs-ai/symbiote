import json, logging
from common import *
logging.disable(logging.CRITICAL)
payload = {"tool": "save_doc", "params": {"doc": {"meta": {"lang": "py"}, "body": "uso:\n```python\nprint(1)\n```"}}}
resp = "```tool_call\n" + json.dumps(payload) + "\n```"
saved = []
llm = FakeLLM(lambda i, m, c, t: resp if i == 0 else "nao posso usar JSON, desculpe")
k = mk_kernel(llm); sym = k.create_symbiote("a", "assistant")
add_tool(k, sym.id, "save_doc", lambda p: saved.append(p) or {"ok": True})
k.environment.configure(sym.id, tools=["save_doc"]); s = k.start_session(sym.id)
out = k.message(s.id, "salve o doc")
print("tool executada:", bool(saved), "| llm_calls:", len(llm.calls), "| retorno:", repr(out)[:90])
print("2a chamada recebeu:", llm.calls[1]["messages"][-1]["content"][:70])
