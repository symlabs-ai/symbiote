"""Run the documented snippets (README / QUICKSTART / HOST_INTEGRATION) against the real package.
cwd must be a scratch dir. No network, MockLLMAdapter only."""
import asyncio
import inspect
import json
import os
import traceback

assert not os.path.exists(".git"), "rode a partir de um diretório temporário, não da raiz do repo"


def step(label, fn):
    try:
        r = fn()
        print(f"[ OK ] {label}" + (f" -> {r}" if r is not None else ""))
        return r
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {label} -> {type(e).__name__}: {str(e).splitlines()[0][:150]}")
        return None


# ── README "Python (library)" block, verbatim ───────────────────────────────
from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel

kernel = SymbioteKernel(
    config=KernelConfig(db_path="data/symbiote.db"),
    llm=MockLLMAdapter(default_response="Hello!"),
)
sym = kernel.create_symbiote(name="Atlas", role="assistant")
session = kernel.start_session(sym.id, goal="Python help")
response = kernel.message(session.id, "How do I use dataclasses?")
kernel.close_session(session.id)
print("[ OK ] README:53-72 library block ->", repr(response))

# ── README "Embedded" block ─────────────────────────────────────────────────
from symbiote.environment.descriptors import HttpToolConfig, ToolDescriptor

bot = sym
user_id, page_url, user_message, visible_text = "u1", "/p", "hi", "text"


def readme_embedded():
    kernel.tool_gateway.register_http_tool(
        ToolDescriptor(tool_id="search", name="Search", description="Search articles",
                       parameters={"type": "object", "properties": {"q": {"type": "string"}}}),
        HttpToolConfig(method="GET", url_template="http://localhost:8000/api/search?q={q}"),
    )
    s = kernel.get_or_create_session(symbiote_id=bot.id, external_key=f"{user_id}:{page_url}")
    kernel.message(s.id, user_message, extra_context={"page_url": page_url, "page_content": visible_text})
    r = kernel.tool_gateway.execute(bot.id, s.id, "search", {"q": "x"})
    return f"then calling the registered tool: success={r.success} error={r.error!r}"


step("README:95-122 embedded block", readme_embedded)

# ── README host integration ─────────────────────────────────────────────────


def readme_recall():
    class MySessionRecall:
        def search_messages(self, query, symbiote_id=None, session_id=None, limit=10):
            return []

        def search_sessions(self, query, symbiote_id=None, limit=5):
            return []
    kernel.set_session_recall(MySessionRecall())


step("README:153-165 set_session_recall", readme_recall)


def readme_hooks():
    from symbiote.core.hooks import BaseHook

    class AuditHook(BaseHook):
        async def before_tool(self, tool_id, params):
            pass

        async def after_tool(self, tool_id, params, result):
            pass
    kernel.hooks.add(AuditHook())


step("README:169-180 hooks", readme_hooks)
step("README:185 prompt_caching", lambda: kernel.environment.configure(symbiote_id=bot.id, prompt_caching=True).prompt_caching)


def readme_bus():
    from symbiote.bus.events import StreamDelta  # noqa: F401
    from symbiote.bus.message_bus import MessageBus
    bus = MessageBus()
    return asyncio.run(bus.receive_delta(timeout=0.05))


step("README:190-205 message bus receive_delta", readme_bus)
step("README:220 store.get_by_category", lambda: len(kernel._memory.get_by_category(sym.id, "procedural")))

# ── README browser block (extras extract/browser NOT installed) ─────────────
from symbiote.browser import register


def readme_browser():
    register(
        kernel,
        search_backend="brave",
        extract_backend=["forge_scraper", "firecrawl"],
        browser_backend="chromium",
        policy={"blocklist": ["*.ads.com", "doubleclick.net"]},
    )
    return sorted(t for t in kernel.tool_gateway.list_tools() if t.startswith(("web_", "browser_")))


tools = step("README:227-240 register(...) verbatim, no extras installed", readme_browser)
print("       web_/browser_ tools now registered:", sorted(t for t in kernel.tool_gateway.list_tools() if t.startswith(("web_", "browser_"))))

# ── QUICKSTART / HOST_INTEGRATION ───────────────────────────────────────────


def qs_register():
    kernel.tool_gateway.register(
        descriptor=ToolDescriptor(tool_id="get_weather", name="Get Weather", description="x", tags=["weather"], risk_level="low"),
        http_config=HttpToolConfig(method="GET", url_template="https://api.weather.com/v1/current?city={city}", headers={"X-API-Key": "..."}),
    )


step("QUICKSTART:177 / HOST_INTEGRATION:72,611 kernel.tool_gateway.register(descriptor=, http_config=)", qs_register)
step("HOST_INTEGRATION:121 kernel.tool_gateway.register(descriptor=, handler=)",
     lambda: kernel.tool_gateway.register(descriptor=ToolDescriptor(tool_id="c", name="c", description="c"), handler=lambda p: "x"))
print("       ToolGateway public register* methods:", [m for m in dir(kernel.tool_gateway) if m.startswith("register")])


def imp(stmt):
    ns = {}
    exec(stmt, ns)  # noqa: S102
    return None


for label, stmt in [
    ("QUICKSTART:206 / HOST_INTEGRATION:106", "from forge_llm.application.tools import McpToolset"),
    ("mcp/provider.py:5 docstring", "from forge_llm.mcp import McpToolset"),
    ("HOST_INTEGRATION:12", "from symbiote.adapters.llm.forge import ForgeLLMAdapter"),
    ("QUICKSTART:351", "from symbiote.harness.benchmark import BenchmarkRunner, BenchmarkTask"),
    ("QUICKSTART:376", "from symbiote.harness.cross_learning import CrossSymbioteLearner"),
    ("HOST_INTEGRATION:523", "from symbiote.harness.tuner import ParameterTuner"),
    ("HOST_INTEGRATION:574", "from symbiote.core.ports import SessionRecallPort"),
    ("HOST_INTEGRATION:658", "from symbiote.harness.evolver import EVOLVABLE_COMPONENTS"),
    ("sdk/client.py:5", "from symbiote.sdk import SymbioteClient"),
]:
    step(f"{label}: {stmt}", lambda s=stmt: imp(s))

step("mcp/provider.py:11 docstring kernel.authorize_tools", lambda: kernel.authorize_tools)


def sig(obj):
    return str(inspect.signature(obj))


from symbiote.runners.chat import ChatRunner

print("       ChatRunner.__init__", sig(ChatRunner.__init__)[:400])
print("       kernel.report_feedback", sig(kernel.report_feedback))
print("       kernel.load_discovered_tools", sig(kernel.load_discovered_tools))
print("       kernel.evolve_harness", sig(kernel.evolve_harness), "| check_harness_rollback", sig(kernel.check_harness_rollback))
step("HOST_INTEGRATION:228 kernel._message_repo.get_messages(session_id, limit=n)",
     lambda: len(kernel._message_repo.get_messages(session.id, limit=12)))


def qs_benchmark():
    from symbiote.harness.benchmark import BenchmarkRunner, BenchmarkTask
    t = BenchmarkTask(id="weather-lookup", description="What is the weather in Tokyo?", expected_tools=["get_weather"], grading="tool_called")
    r = BenchmarkRunner(kernel)
    return sig(r.run_suite) + " | task=" + str(t)[:80]


step("QUICKSTART:351-363 BenchmarkTask/BenchmarkRunner", qs_benchmark)


def qs_cross():
    from symbiote.harness.cross_learning import CrossSymbioteLearner
    learner = CrossSymbioteLearner(kernel._storage, kernel.harness_versions)
    return sig(learner.find_candidates) + " | transfer" + sig(learner.transfer)


step("QUICKSTART:376-382 CrossSymbioteLearner", qs_cross)


def hi_tuner():
    from symbiote.harness.tuner import ParameterTuner
    tuner = ParameterTuner(kernel._storage)
    result = tuner.analyze(symbiote_id=sym.id, days=7)
    _ = (result.session_count, result.tier, result.adjustments)
    return sig(tuner.apply)


step("HOST_INTEGRATION:523-537 ParameterTuner", hi_tuner)
step("HOST_INTEGRATION:348 report_feedback", lambda: kernel.report_feedback(session.id, score=1.0, source="thumbs_up"))
step("HOST_INTEGRATION:374 set_approval_callback", lambda: kernel.set_approval_callback(lambda t, p, r: True))


def hi_cfg():
    kernel.environment.configure(symbiote_id=sym.id, tool_mode="long_run", planner_prompt="p", evaluator_prompt="e",
                                 evaluator_criteria=[{"name": "c", "weight": 1.0, "threshold": 0.7, "description": "d"}],
                                 context_strategy="hybrid", max_blocks=15)
    kernel.environment.configure(symbiote_id=sym.id, tool_mode="continuous")
    kernel.environment.configure(symbiote_id=sym.id, tool_mode="brief", max_tool_iterations=15, context_mode="packed", memory_share=0.35, knowledge_share=0.30)
    kernel.environment.configure(symbiote_id=sym.id, tool_call_timeout=15.0, loop_timeout=120.0)


step("HOST_INTEGRATION:450-499,627 configure(...) variants", hi_cfg)


async def hi_async():
    toks = []
    r = await kernel.message_async(kernel.start_session(sym.id).id, "hi", extra_context={"url": "/x"}, on_token=lambda t: toks.append(t))
    return repr(r)[:40]


step("HOST_INTEGRATION:643 message_async(on_token=)", lambda: asyncio.run(hi_async()))

# ── QUICKSTART:244-251 on_demand: tools promised to the LLM vs. policy ─────
sym2 = kernel.create_symbiote(name="OnDemand", role="assistant")
kernel.environment.configure(symbiote_id=sym2.id, context_mode="on_demand")
kernel._llm.default_response = "final"
kernel._llm._responses = ['```tool_call\n{"tool": "search_memories", "params": {"query": "x"}}\n```', "done"]
kernel._llm._response_index = 0
kernel._llm.calls.clear()
s2 = kernel.start_session(sym2.id)
r = kernel.message(s2.id, "what do you remember?")
sysmsg = kernel._llm.calls[0]["messages"][0]["content"]
print("[INFO] QUICKSTART:247 configure(context_mode='on_demand'): system prompt promises the tools:",
      "You have access to search_memories and search_knowledge tools" in sysmsg)
print("       authorized tools:", kernel.environment.list_tools(sym2.id))
last = json.dumps(kernel._llm.calls[1]["messages"][-1], ensure_ascii=False)
print("       tool result fed back to the LLM:", last[:260])

kernel.shutdown()
