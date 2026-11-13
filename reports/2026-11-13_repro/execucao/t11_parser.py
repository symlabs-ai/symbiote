"""parse_tool_calls fragility."""
import json, logging
from common import *
from symbiote.environment.parser import parse_tool_calls
logging.disable(logging.CRITICAL)
cases = {
 "array em vez de objeto": '```tool_call\n[{"tool": "save_note", "params": {"t": "a"}}]\n```',
 "params null": '```tool_call\n{"tool": "save_note", "params": null}\n```',
 "params string": '```tool_call\n{"tool": "save_note", "params": "abc"}\n```',
 "tool nao-string": '```tool_call\n{"tool": 123, "params": {}}\n```',
 "JSON escalar": '```tool_call\n"save_note"\n```',
 "params com cerca ``` (markdown no conteudo)": '```tool_call\n' + json.dumps({"tool": "save_note", "params": {"text": "exemplo:\n```python\nprint(1)\n```\nfim"}}) + '\n```',
 "2 blocos, 2o malformado": tc("save_note", t="a") + '\n```tool_call\n{"tool": "save_note", "params": {"t": "b",}}\n```',
}
for name, text in cases.items():
    try:
        clean, calls = parse_tool_calls(text)
        print(f"{name}: calls={[(c.tool_id, c.params) for c in calls]} clean={clean[:60]!r}")
    except Exception as e:
        print(f"{name}: EXCEPTION {type(e).__name__}: {str(e)[:90]!r}")

print("--- via kernel.message ---")
saved = []
for name in ("array em vez de objeto", "params com cerca ``` (markdown no conteudo)"):
    llm = FakeLLM(lambda i, m, c, t, name=name: cases[name])
    k = mk_kernel(llm)
    sym = k.create_symbiote("a", "assistant")
    add_tool(k, sym.id, "save_note", lambda p: saved.append(p) or {"ok": True})
    k.environment.configure(sym.id, tools=["save_note"])
    s = k.start_session(sym.id)
    try:
        out = k.message(s.id, "salve a nota")
        print(f"{name}: retorno={out!r} llm_calls={len(llm.calls)} tool_executada={bool(saved)}")
    except Exception as e:
        print(f"{name}: kernel.message levantou {type(e).__name__}: {e}")
    print("   mensagens persistidas:", [(m.role, m.content[:25]) for m in reversed(k._sessions.get_messages(s.id, limit=5))])
