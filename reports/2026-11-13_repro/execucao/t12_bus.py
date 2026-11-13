"""MessageBus handler retry x non-idempotent kernel.message_async; head-of-line blocking; swallowed error."""
import asyncio, logging, time, io
from common import *
from symbiote.bus.message_bus import MessageBus
from symbiote.bus.events import InboundMessage, OutboundMessage

buf = io.StringIO(); h = logging.StreamHandler(buf); logging.getLogger("symbiote.bus.message_bus").addHandler(h)
logging.getLogger("symbiote").setLevel(logging.WARNING)

charges = []
def fn(i, messages, config, tools):
    if "[Tool result: charge_card]" in messages[-1]["content"]:
        raise RuntimeError("400 invalid_request: context_length_exceeded")   # LLM fails AFTER the tool ran
    return tc("charge_card", amount=100)

async def main():
    llm = FakeLLM(fn)
    k = mk_kernel(llm)
    sym = k.create_symbiote("a", "assistant")
    add_tool(k, sym.id, "charge_card", lambda p: charges.append(p) or {"charged": p["amount"]})
    k.environment.configure(sym.id, tools=["charge_card"])
    s = k.start_session(sym.id)
    bus = MessageBus(base_delay=0.2)          # default is 1.0 -> 1s+2s+4s
    done = {}
    async def handler(msg: InboundMessage):
        if msg.chat_id == "other":
            done["other"] = time.monotonic(); return
        out = await k.message_async(s.id, msg.content)
        await bus.respond(OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=str(out)))
    bus.on_inbound(handler)
    await bus.start()
    t0 = time.monotonic()
    await bus.publish(InboundMessage(channel="http", chat_id="c1", symbiote_id=sym.id, content="cobre 100"))
    await bus.publish(InboundMessage(channel="http", chat_id="other", symbiote_id=sym.id, content="oi"))
    await asyncio.sleep(2.5)
    await bus.stop()
    msgs = [(m.role, m.content) for m in reversed(k._sessions.get_messages(s.id, limit=20))]
    print("execucoes da tool charge_card:", len(charges))
    print("mensagens persistidas na sessao:", msgs)
    print("resposta outbound entregue:", bus.outbound_size)
    print(f"mensagem de OUTRO chat so foi atendida apos {done['other']-t0:.2f}s (com base_delay default seria >=7s)")
    print("log do bus (nao contem a excecao):\n  " + buf.getvalue().strip().replace("\n", "\n  "))
asyncio.run(main())
