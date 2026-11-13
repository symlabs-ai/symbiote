"""Concrete behaviour differences between ChatRunner.run() and run_async()."""
import asyncio, logging
from common import *
from symbiote.runners.chat import ChatRunner
from symbiote.core.context import AssembledContext
logging.disable(logging.CRITICAL)

def fn(i, messages, config, tools):
    return tc("create_order", item="x", n=i) if "[Tool result" not in messages[-1]["content"] else "LLM final text"

class Review:
    def __init__(self): self.spawned = 0
    def spawn(self, session_id, symbiote_id): self.spawned += 1

def build():
    llm = FakeLLM(fn)
    k = mk_kernel(llm)
    sym = k.create_symbiote("a", "assistant")
    add_tool(k, sym.id, "create_order", lambda p: {"order_id": 42})
    k.environment.configure(sym.id, tools=["create_order"])
    review = Review()
    hook_calls = []
    runner = ChatRunner(llm, tool_gateway=k.tool_gateway,
                        on_after_tool_result=lambda calls, results: (hook_calls.append(1), "HOOK FINAL TEXT")[1],
                        background_review=review, skill_nudge_interval=1)
    s = k.start_session(sym.id)
    ctx = k._context_assembler.build(session_id=s.id, symbiote_id=sym.id, user_input="crie o pedido")
    return llm, runner, ctx, review, hook_calls

llm, runner, ctx, review, hook_calls = build()
r = runner.run(ctx)
print(f"run()      : text={r.output['text']!r} stop={r.loop_trace.stop_reason} llm_calls={len(llm.calls)} hook_calls={len(hook_calls)} skill_review_spawns={review.spawned}")
llm, runner, ctx, review, hook_calls = build()
r = asyncio.run(runner.run_async(ctx))
print(f"run_async(): text={r.output['text']!r} stop={r.loop_trace.stop_reason} llm_calls={len(llm.calls)} hook_calls={len(hook_calls)} skill_review_spawns={review.spawned}")

# max_tool_iterations=0 (AssembledContext accepts it; no validator)
llm, runner, ctx, review, hook_calls = build()
ctx0 = ctx.model_copy(update={"max_tool_iterations": 0})
print("run() max_iters=0      ->", runner.run(ctx0).success)
try:
    asyncio.run(runner.run_async(ctx0))
except Exception as e:
    print("run_async() max_iters=0 ->", type(e).__name__, e)
