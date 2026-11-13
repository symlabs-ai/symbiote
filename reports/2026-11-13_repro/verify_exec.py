import os, tempfile, logging, asyncio
d = tempfile.mkdtemp(prefix="symbiote-repro-", dir=os.environ.get("PROBE_TMP")); os.chdir(d)
logging.disable(logging.CRITICAL)
from pathlib import Path
from symbiote.core.kernel import SymbioteKernel
from symbiote.config.models import KernelConfig
from symbiote.core.hooks import BaseHook
from symbiote.environment.descriptors import ToolDescriptor
from symbiote.runners.response_validator import is_valid_response
from symbiote.environment.parser import parse_tool_calls
TC = lambda tool, **p: '```tool_call\n' + __import__("json").dumps({"tool": tool, "params": p}) + '\n```'
class LLM:
    def __init__(self, fn): self.fn, self.calls = fn, []
    def complete(self, messages, config=None, tools=None, **kw):
        self.calls.append(messages); return self.fn(len(self.calls) - 1, messages)
def kernel(fn, n=[0]):
    n[0] += 1; llm = LLM(fn); return SymbioteKernel(KernelConfig(db_path=Path(d) / f"t{n[0]}.db"), llm=llm), llm
def tool(k, sid, tid, h, **cfg):
    k.tool_gateway.register_descriptor(ToolDescriptor(tool_id=tid, name=tid, description=tid, parameters={"type": "object", "properties": {}}), h)
    k.environment.configure(symbiote_id=sid, tools=[tid], **cfg)

print("1. validador:", {t[:22]: is_valid_response(t) for t in ["Fontes:\n[1] Relatório 2025\n[2] Nota 14", "[Docs](https://x.y) explicam.", "- item\n[x] feito", "Tudo certo, pedido criado."]})
for bad in ['```tool_call\n[{"tool": "t", "params": {}}]\n```', '```tool_call\n{"tool": "t", "params": null}\n```']:
    try: print("2. parser:", parse_tool_calls(bad))
    except Exception as e: print("2. parser levantou", type(e).__name__)

# 3. hooks
ev = []
class Audit(BaseHook):
    async def before_tool(self, tool_id, params): ev.append(("before", tool_id))
    async def after_tool(self, tool_id, params, result): ev.append(("after", tool_id))
k, llm = kernel(lambda i, m: TC("echo", x=1) if i == 0 else "feito")
s = k.create_symbiote(name="a", role="assistant"); ran = []
tool(k, s.id, "echo", lambda p: ran.append(p) or {"ok": True}); k.hooks.add(Audit())
sess = k.start_session(s.id); out = k.message(sess.id, "use a tool")
print(f"3. hooks: tool executada {len(ran)}x, resposta={out!r}, eventos recebidos pelo hook={ev}")

# 4. instant + handler async
async def ah(p): await asyncio.sleep(0.01); return {"order_id": 42}
for mode in ("brief", "instant"):
    k, llm = kernel(lambda i, m: TC("create_order", item="x") if i == 0 else "Pedido criado.")
    s = k.create_symbiote(name="a", role="assistant"); tool(k, s.id, "create_order", ah, tool_mode=mode)
    sess = k.start_session(s.id)
    import warnings; warnings.simplefilter("ignore")
    out = asyncio.run(k.message_async(sess.id, "crie"))
    row = k._storage.fetch_one("SELECT result FROM audit_log WHERE tool_id='create_order' ORDER BY created_at DESC LIMIT 1")
    print(f"4. message_async tool_mode={mode:8}: resposta={out if isinstance(out,str) else out.get('text')!r} audit={row and row['result']}")

# 5. autocompact com system prompt grande
def fn(i, m):
    blob = "\n".join(str(x.get("content")) for x in m[1:])
    if "SECRET-4711" in blob: return "O saldo é SECRET-4711"
    return "não consegui obter o saldo" if i >= 6 else TC("get_balance", account=f"acc-{i}")
for persona in (2_000, 60_000):
    k, llm = kernel(fn); s = k.create_symbiote(name="a", role="assistant", persona={"instructions": "x" * persona}); n = []
    tool(k, s.id, "get_balance", lambda p: n.append(1) or {"balance": "SECRET-4711"})
    sess = k.start_session(s.id); out = k.message(sess.id, "qual o saldo?")
    print(f"5. persona={persona:6} chars: chamadas LLM={len(llm.calls)} execuções da tool={len(n)} resposta={out!r}")

# 6. max_iterations devolve vazio
k, llm = kernel(lambda i, m: TC("fetch", n=i)); s = k.create_symbiote(name="a", role="assistant")
tool(k, s.id, "fetch", lambda p: {"page": p}, max_tool_iterations=3); sess = k.start_session(s.id)
print(f"6. loop esgotado: resposta={k.message(sess.id, 'pesquise')!r}")
