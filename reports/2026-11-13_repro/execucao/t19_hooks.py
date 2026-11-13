import asyncio, logging
from common import *
from symbiote.core.hooks import BaseHook
from symbiote.runners.loop_control import LoopController
logging.disable(logging.CRITICAL)
events = []
class Audit(BaseHook):
    async def before_tool(self, tool_id, params): events.append(("before_tool", tool_id))
    async def after_tool(self, tool_id, params, result): events.append(("after_tool", tool_id))
    async def before_turn(self, messages): events.append(("before_turn",))
    async def after_turn(self, messages, response): events.append(("after_turn",))
llm = FakeLLM(lambda i, m, c, t: tc("echo", x=1) if i % 2 == 0 else "feito")
k = mk_kernel(llm); k.hooks.add(Audit())
sym = k.create_symbiote("a", "assistant"); add_tool(k, sym.id, "echo", lambda p: {"ok": 1})
k.environment.configure(sym.id, tools=["echo"]); s = k.start_session(sym.id)
k.message(s.id, "use a tool"); asyncio.run(k.message_async(s.id, "use a tool de novo"))
print("tool executada 2x (sync+async); eventos recebidos pelo hook registrado em kernel.hooks:", events)

# circuit-breaker override with braces (text comes from harness_versions, i.e. evolvable/LLM-written)
c = LoopController(circuit_breaker_msg='Tool {tool} failed. Reply with {"status": "unavailable"}.')
for _ in range(3): c.record("fetch", {"n": _}, False)
print("should_stop:", c.should_stop())
try: c.get_injection_message()
except Exception as e: print("get_injection_message ->", type(e).__name__, e)
