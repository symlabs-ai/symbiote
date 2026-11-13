"""ContextAssembler budget ignores tool schemas, extra_context and the persona is never trimmed."""
import logging
from common import *
logging.disable(logging.CRITICAL)
llm = FakeLLM(lambda i, m, c, t: "ok")
k = mk_kernel(llm, context_budget=4000)
sym = k.create_symbiote("a", "assistant", persona={"role": "helper"})
schema = {"type": "object", "properties": {f"p{j}": {"type": "string", "description": "d" * 80} for j in range(10)}}
for i in range(40):
    add_tool(k, sym.id, f"tool_{i}", lambda p: {}, params=schema)
k.environment.configure(sym.id, tools=[f"tool_{i}" for i in range(40)])
s = k.start_session(sym.id)
extra = {"page": "conteudo da pagina " * 3000}
ctx = k._context_assembler.build(session_id=s.id, symbiote_id=sym.id, user_input="oi", extra_context=extra)
insp = k._context_assembler.inspect(ctx)
k.message(s.id, "oi", extra_context=extra)
real = sum(len(m["content"]) for m in llm.calls[0]["messages"]) // 4
print(f"budget={insp.budget} total_tokens_estimate={insp.total_tokens_estimate} within_budget={insp.within_budget} | prompt real enviado ~{real} tokens ({real/insp.budget:.0f}x o budget)")
