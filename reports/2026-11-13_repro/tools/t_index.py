import json, sys
from pathlib import Path
from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel
from symbiote.environment.descriptors import ToolDescriptor
HERE = Path(sys.argv[1])
for f in HERE.glob("i.db*"): f.unlink()
k = SymbioteKernel(config=KernelConfig(db_path=HERE / "i.db"), llm=MockLLMAdapter())
gw = k.tool_gateway
gw.register_descriptor(ToolDescriptor(tool_id="get_weather", name="W", description="weather", tags=["weather"],
    parameters={"type": "object", "properties": {"city": {"type": "string"}}}), lambda p: "sunny")
gw.register_descriptor(ToolDescriptor(tool_id="admin_wipe", name="Wipe", description="wipe", tags=["admin"], risk_level="high",
    parameters={"type": "object", "properties": {"confirm_token": {"type": "string"}}}), lambda p: "wiped")
a = k.create_symbiote(name="A", role="assistant")
# kernel.configure_tool_visibility as documented in its docstring
k.environment.configure(symbiote_id=a.id, tools=["get_weather"])
k.configure_tool_visibility(a.id, tags=["weather"], loading="index")
k._llm._responses = ['```tool_call\n{"tool": "get_tool_schema", "params": {"tool_id": "get_weather"}}\n```', "done"]
s = k.start_session(a.id)
k.message(s.id, "weather in Tokyo?")
sysmsg = k._llm.calls[0]["messages"][0]["content"]
print("index mode: prompt tells the LLM to call get_tool_schema:", "get_tool_schema" in sysmsg)
print("  authorized tools:", k.environment.list_tools(a.id))
print("  result fed back:", k._llm.calls[1]["messages"][-1]["content"][:150].replace("\n", " | "))
k.environment.configure(symbiote_id=a.id, tools=["get_weather", "get_tool_schema"])
r = gw.execute(a.id, None, "get_tool_schema", {"tool_id": "admin_wipe"})
print("  once authorized, get_tool_schema(tool not authorized / outside tool_tags) ->", json.dumps(r.output)[:140])
# tags vs untagged builtins in on_demand
k.environment.configure(symbiote_id=a.id, tools=["get_weather", "search_memories"], tool_loading="full", context_mode="on_demand")
ctx = k._context_assembler.build(session_id=s.id, symbiote_id=a.id, user_input="x")
print("on_demand + tool_tags=['weather']: tools shown to LLM:", [t["tool_id"] for t in ctx.available_tools] if hasattr(ctx, "available_tools") else [n for n in dir(ctx) if "tool" in n])
k.shutdown()
