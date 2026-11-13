"""long_run mode."""
import json, logging
from common import *
from symbiote.runners.long_run import LongRunRunner
from symbiote.core.context import AssembledContext
from symbiote.environment.descriptors import LLMResponse
logging.disable(logging.CRITICAL)

PLAN = json.dumps([{"name": "A", "description": "d", "success_criteria": "c"}, {"name": "B", "description": "d", "success_criteria": "c"}])
# (1) via kernel: tool_mode='long_run' + planner_prompt configured
calls = []
def fn(i, messages, config, tools):
    calls.append(messages[0]["content"][:40]); return "resposta comum de chat"
k = mk_kernel(FakeLLM(fn))
sym = k.create_symbiote("a", "assistant")
k.environment.configure(sym.id, tool_mode="long_run", planner_prompt="PLANNER-PROMPT: decompose", evaluator_prompt="EVAL")
s = k.start_session(sym.id)
out = k.message(s.id, "construa o sistema inteiro")
print("(1) kernel tool_mode=long_run ->", repr(out), "| runners registrados:", k._runner_registry.list_runners(),
      "| planner chamado:", any("PLANNER-PROMPT" in c for c in calls), "| handoff:", k._capabilities.last_handoff_data)

ctx = AssembledContext(symbiote_id="s", session_id="x", user_input="construa", evaluator_prompt="EVAL-PROMPT")
# (2) evaluator always rejects
def fn2(i, messages, config, tools):
    sysm = messages[0]["content"]
    if "project planner" in sysm: return PLAN
    if sysm.startswith("EVAL-PROMPT"): return json.dumps({"passed": False, "overall_score": 0.1, "feedback": "nada foi entregue", "blocking_issues": ["x"]})
    return "bloco feito (alegacao do gerador)"
r = LongRunRunner(FakeLLM(fn2)).run(ctx)
print("(2) avaliador reprova 100%:", "success=", r.success, "| stop=", r.loop_trace.stop_reason, "|", r.output.replace("\n", " / "))
print("    pending_blocks no handoff:", r.handoff_data["pending_blocks"], "blocks_completed:", r.handoff_data["blocks_completed"])

# (3) planner adapter returns LLMResponse (ForgeLLMAdapter does so whenever reasoning_content is present)
def fn3(i, messages, config, tools):
    return LLMResponse(content=PLAN, reasoning_content="thinking...")
r = LongRunRunner(FakeLLM(fn3)).run(AssembledContext(symbiote_id="s", session_id="x", user_input="construa"))
print("(3) planner com LLMResponse:", r.success, r.error, "| raw_spec:", r.plan.raw_spec[:60])

# (4) signature incompatible with CapabilitySurface.chat (runner.run(context, llm_config=...))
try:
    LongRunRunner(FakeLLM(fn2)).run(ctx, llm_config=None)
except TypeError as e:
    print("(4)", e)
print("(5) hasattr(AssembledContext, 'tools') =", hasattr(ctx, "tools"), "-> _preflight_tools sempre None")
