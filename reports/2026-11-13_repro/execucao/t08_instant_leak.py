"""Instant mode + streaming adapter: on_token gets the raw tool_call block."""
import asyncio, logging
from common import *
logging.disable(logging.CRITICAL)

class StreamLLM(FakeLLM):
    def stream(self, messages, config=None, tools=None):
        text = self.complete(messages, config, tools)
        for i in range(0, len(text), 12):
            yield text[i:i+12]

for mode in ("instant", "brief"):
    llm = StreamLLM(lambda i, m, c, t: "Vou criar.\n" + tc("create_order", item="x") if i == 0 else "Pedido 42 criado.")
    k = mk_kernel(llm)
    sym = k.create_symbiote("a", "assistant")
    add_tool(k, sym.id, "create_order", lambda p: {"order_id": 42})
    k.environment.configure(sym.id, tools=["create_order"], tool_mode=mode)
    s = k.start_session(sym.id)
    toks = []
    out = asyncio.run(k.message_async(s.id, "crie o pedido", on_token=toks.append))
    print(f"mode={mode}: retorno.text={out['text']!r}\n   streamed via on_token={''.join(toks)!r}")
