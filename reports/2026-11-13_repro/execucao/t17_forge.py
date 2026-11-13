"""ForgeLLMAdapter edge cases, using a stub ChatAgent (no network)."""
import logging, types
from common import *
from symbiote.adapters.llm import forge
from symbiote.adapters.llm.forge import ForgeLLMAdapter, _extract_tool_calls
logging.disable(logging.CRITICAL)

# (a) truncated arguments (finish_reason=length) -> params silently become {}
resp = types.SimpleNamespace(content="", tool_calls=[{"id": "c1", "type": "function",
        "function": {"name": "delete_records", "arguments": '{"filter": {"status": "arch'}}], metadata=None)
print("(a)", [(c.tool_id, c.params) for c in _extract_tool_calls(resp)])

class StubAgent:
    def __init__(self, resp): self.resp = resp
    def chat(self, **kw): return self.resp
def adapter(resp):
    a = ForgeLLMAdapter.__new__(ForgeLLMAdapter); a._agent = StubAgent(resp); return a

# end-to-end: the tool runs with {} (all-optional params => "delete everything")
deleted = []
k = mk_kernel(adapter(resp))
k.set_native_tools(True)
sym = k.create_symbiote("a", "assistant")
add_tool(k, sym.id, "delete_records", lambda p: deleted.append(p) or {"deleted": "ALL" if not p.get("filter") else "filtered"},
         params={"type": "object", "properties": {"filter": {"type": "object"}}})
k.environment.configure(sym.id, tools=["delete_records"], tool_mode="instant")
s = k.start_session(sym.id)
out = k.message(s.id, "apague os arquivados")
print("    tool executada com params:", deleted, "->", out["tool_results"][0]["output"])

# (b) content=None and no tool calls -> complete() returns None -> runner crashes in the regex parser
k = mk_kernel(adapter(types.SimpleNamespace(content=None, tool_calls=None, metadata=None)))
sym = k.create_symbiote("a", "assistant"); s = k.start_session(sym.id)
try: print("(b)", k.message(s.id, "oi"))
except Exception as e: print("(b) kernel.message levantou", type(e).__name__, e)

# (c) arguments that decode to a non-dict
for args in ('null', '[1,2]', '"x"'):
    r = types.SimpleNamespace(content="", metadata=None, tool_calls=[{"id": "c1", "function": {"name": "t", "arguments": args}}])
    try: print("(c)", args, "->", _extract_tool_calls(r)[0].params)
    except Exception as e: print("(c)", args, "->", type(e).__name__)
