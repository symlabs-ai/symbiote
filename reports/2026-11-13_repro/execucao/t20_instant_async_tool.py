import asyncio, logging, warnings
from common import *
logging.disable(logging.CRITICAL); warnings.simplefilter("ignore")
async def ahandler(p):
    await asyncio.sleep(0.01); return {"order_id": 42}
for mode in ("brief", "instant"):
    llm = FakeLLM(lambda i, m, c, t: tc("create_order", item="x") if i == 0 else "Pedido criado.")
    k = mk_kernel(llm)
    sym = k.create_symbiote("a", "assistant")
    add_tool(k, sym.id, "create_order", ahandler)
    k.environment.configure(sym.id, tools=["create_order"], tool_mode=mode)
    s = k.start_session(sym.id)
    try:
        out = asyncio.run(k.message_async(s.id, "crie"))
        print(f"message_async mode={mode}: text={out['text']!r} tool_result={ {k_: v for k_, v in out['tool_results'][0].items() if k_ in ('success','output','error')} }")
    except Exception as e:
        print(f"message_async mode={mode}: EXC {type(e).__name__}: {e}")
