"""Layer-2 compaction: the 2nd compaction loses the 1st summary."""
import logging, json
from common import *
from symbiote.environment.descriptors import LLMResponse, NativeToolCall
logging.disable(logging.CRITICAL)
N = 8
def text_fn(i, messages, config, tools):
    return tc("step", n=i) if i < N else "fim"
def native_fn(i, messages, config, tools):
    if i < N:
        return LLMResponse(content="", tool_calls=[NativeToolCall(call_id=f"c{i}", tool_id="step", params={"n": i})])
    return "fim"

for label, fn, native in (("legacy_text", text_fn, False), ("native", native_fn, True)):
    llm = FakeLLM(fn)
    k = mk_kernel(llm)
    k.set_native_tools(native)
    sym = k.create_symbiote("a", "assistant")
    add_tool(k, sym.id, "step", lambda p: {"value": f"RESULT-OF-STEP-{p['n']}"}, params={"type": "object", "properties": {"n": {"type": "integer"}}})
    k.environment.configure(sym.id, tools=["step"])
    s = k.start_session(sym.id)
    k.message(s.id, "execute 8 passos")
    last = llm.calls[-1]["messages"]
    blob = json.dumps(last[2:], ensure_ascii=False)
    visible = [n for n in range(N) if f"RESULT-OF-STEP-{n}" in blob]
    print(f"== {label}: llm_calls={len(llm.calls)}; na ultima chamada o modelo ainda ve resultados dos passos: {visible} (de 0..{N-1})")
    for m in last[2:]:
        c = m.get("content") or ""
        print("   ", m["role"], "|", c[:330].replace("\n", "\\n"), ("| tool_calls=" + str(len(m["tool_calls"]))) if m.get("tool_calls") else "")
