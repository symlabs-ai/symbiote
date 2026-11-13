"""Gateway / PolicyGate / EnvironmentManager probes."""
import asyncio
import os
import sys
import threading
import time
from pathlib import Path

from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel
from symbiote.environment.descriptors import ToolCall, ToolDescriptor

HERE = Path(sys.argv[1])
db = HERE / "gw.db"
for f in HERE.glob("gw.db*"):
    f.unlink()

kernel = SymbioteKernel(config=KernelConfig(db_path=db), llm=MockLLMAdapter())
gw = kernel.tool_gateway
a = kernel.create_symbiote(name="A", role="assistant")
b = kernel.create_symbiote(name="B", role="assistant")

print("== 1. async handler through the sync path ==")


async def async_tool(params):
    return {"real": "result"}


gw.register_descriptor(ToolDescriptor(tool_id="async_tool", name="x", description="x"), async_tool)
kernel.environment.configure(symbiote_id=a.id, tools=["async_tool", "slow", "marker", "search_memories", "search_knowledge", "bash"])
r = gw.execute(a.id, None, "async_tool", {})
print("  gateway.execute        ->", r.success, repr(r.output)[:70])
rs = gw.execute_tool_calls(a.id, None, [ToolCall(tool_id="async_tool"), ToolCall(tool_id="async_tool")])
print("  execute_tool_calls x2  ->", [(x.success, repr(x.output)[:40]) for x in rs])
r = asyncio.run(gw.execute_async(a.id, None, "async_tool", {}))
print("  execute_async          ->", r.success, r.output)

print("\n== 2. async timeout: returns on time, work keeps running ==")
marker = HERE / "marker.txt"
marker.unlink(missing_ok=True)


def slow_side_effect(params):
    time.sleep(1.5)
    marker.write_text("written after timeout")
    return "done"


gw.register_descriptor(ToolDescriptor(tool_id="slow", name="x", description="x"), slow_side_effect)


async def run_async():
    t0 = time.perf_counter()
    r = await gw.execute_async(a.id, None, "slow", {}, timeout=0.2)
    t1 = time.perf_counter() - t0
    print(f"  execute_async returned in {t1:.2f}s success={r.success} error={r.error}")
    print("  marker exists right after return:", marker.exists(), "| live threads:", threading.active_count())
    await asyncio.sleep(1.6)
    print("  marker exists 1.6s later:", marker.exists(), "->", marker.read_text() if marker.exists() else None)


t0 = time.perf_counter()
asyncio.run(run_async())
print(f"  (asyncio.run total {time.perf_counter() - t0:.2f}s)")

print("\n== 3. bash tool: own timeout vs child processes ==")
t0 = time.perf_counter()
r = gw.execute(a.id, None, "bash", {"command": "sleep 4; echo late", "timeout": 1}, timeout=30)
print(f"  bash timeout=1, 'sleep 4; echo late' -> {time.perf_counter() - t0:.2f}s output={r.output}")
t0 = time.perf_counter()
r = gw.execute(a.id, None, "bash", {"command": "sleep 3", "timeout": 100000}, timeout=0.5)
print(f"  gateway timeout=0.5, bash timeout=100000, 'sleep 3' -> {time.perf_counter() - t0:.2f}s err={r.error}")
r = gw.execute(a.id, None, "bash", {"command": "pwd", "_cwd": "/etc"})
print("  LLM-supplied _cwd=/etc ->", r.output)

print("\n== 4. sync parallel path: per-call timeout semantics ==")


def sleeper(params):
    time.sleep(params["s"])
    return f"slept {params['s']}"


gw.register_descriptor(ToolDescriptor(tool_id="marker", name="x", description="x"), sleeper)
calls = [ToolCall(tool_id="marker", params={"s": 0.9}) for _ in range(3)]
t0 = time.perf_counter()
rs = gw.execute_tool_calls(a.id, None, calls, timeout=0.5)
print(f"  3 calls of 0.9s, timeout=0.5 -> {time.perf_counter() - t0:.2f}s:", [(x.success, (x.output or x.error)[:28]) for x in rs])
calls = [ToolCall(tool_id="marker", params={"s": 0.6}) for _ in range(6)]
t0 = time.perf_counter()
rs = gw.execute_tool_calls(a.id, None, calls, timeout=1.0)
print(f"  6 calls of 0.6s, timeout=1.0 -> {time.perf_counter() - t0:.2f}s:", [x.success for x in rs])

print("\n== 5. memory tools: scoping ==")
sa = kernel.start_session(a.id)
sb = kernel.start_session(b.id)
kernel.capabilities.learn(symbiote_id=b.id, session_id=sb.id, content="B-ONLY secret: vault code 4411", fact_type="factual", importance=0.9)
r = gw.execute(a.id, sa.id, "search_memories", {"query": "vault"})
print("  symbiote A search_memories('vault') ->", r.success, r.output, r.error)
rs = gw.execute_tool_calls(a.id, sa.id, [ToolCall(tool_id="search_memories", params={"query": "vault"}),
                                         ToolCall(tool_id="search_knowledge", params={"query": "vault"})])
print("  parallel path ->", [(x.success, x.output, (x.error or "")[:60]) for x in rs])
try:
    kernel._knowledge.register_source(symbiote_id=a.id, name="doc", content="vault procedures for A")
except Exception as e:  # noqa: BLE001
    print("  (register_source failed:", type(e).__name__, e, ")")
r = gw.execute(a.id, sa.id, "search_knowledge", {"query": "vault"})
print("  A search_knowledge('vault') as the LLM would call it ->", r.success, r.output, r.error)
r = gw.execute(b.id, sb.id, "search_knowledge", {"query": "vault", "symbiote_id": a.id})
print("  B (not even authorized?) passing symbiote_id=A ->", r.success, r.output, (r.error or "")[:70])
kernel.environment.configure(symbiote_id=b.id, tools=["search_knowledge"])
r = gw.execute(b.id, sb.id, "search_knowledge", {"query": "vault", "symbiote_id": a.id})
print("  B authorized, passing symbiote_id=A ->", r.success, r.output)

print("\n== 6. EnvironmentManager.configure ==")
env = kernel.environment
c = kernel.create_symbiote(name="C", role="assistant")
cfg = env.configure(symbiote_id=c.id, tools=["fs_read"], memory_share=0.0, knowledge_share=0.0)
got = env.get_config(c.id)
print(f"  configure(memory_share=0.0, knowledge_share=0.0) returned {cfg.memory_share}/{cfg.knowledge_share}; get_config reads {got.memory_share}/{got.knowledge_share}")
row = kernel._storage.fetch_one("SELECT memory_share, knowledge_share FROM environment_configs WHERE symbiote_id=?", (c.id,))
print("  stored row:", dict(row))
env.configure(symbiote_id=c.id, tool_mode="instant")
env.configure(symbiote_id=c.id, tool_loop=True)
got = env.get_config(c.id)
print(f"  tool_mode='instant' then tool_loop=True -> tool_mode={got.tool_mode!r} tool_loop={got.tool_loop} get_tool_loop={env.get_tool_loop(c.id)}")
env.configure(symbiote_id=c.id, workspace_id="ws1", prompt_caching=True)
print("  symbiote-level tools:", env.list_tools(c.id), "| after configure(workspace_id='ws1', prompt_caching=True):", env.list_tools(c.id, "ws1"),
      "| is_tool_enabled(fs_read, ws1) =", env.is_tool_enabled(c.id, "fs_read", "ws1"))
for kw in ({"tool_call_timeout": 0.5}, {"tool_loading": "bogus"}, {"max_tool_iterations": 51}, {"max_tool_iterations": 0}, {"memory_share": 1.5}, {"tools": "fs_read"}):
    try:
        r = env.configure(symbiote_id=c.id, **kw)
        print(f"  configure({kw}) -> accepted: tools={r.tools if 'tools' in kw else ''}")
    except Exception as e:  # noqa: BLE001
        print(f"  configure({kw}) -> {type(e).__name__}: {str(e).splitlines()[0][:80]}")
print("  list_tools after tools='fs_read':", env.list_tools(c.id), "| is_tool_enabled('fs'):", env.is_tool_enabled(c.id, "fs"), "| ('r'):", env.is_tool_enabled(c.id, "r"))
try:
    env.configure(symbiote_id="ghost", tools=["x"])
    print("  configure(symbiote_id='ghost') accepted")
except Exception as e:  # noqa: BLE001
    print("  configure(symbiote_id='ghost') ->", type(e).__name__, e)

print("\n== 7. duplicate rows for (symbiote, NULL workspace) ==")
d = kernel.create_symbiote(name="D", role="assistant")
idx = kernel._storage.fetch_all("SELECT name, sql FROM sqlite_master WHERE tbl_name='environment_configs' AND type='index'")
print("  indexes on environment_configs:", [dict(i) for i in idx])
kernel.shutdown()
