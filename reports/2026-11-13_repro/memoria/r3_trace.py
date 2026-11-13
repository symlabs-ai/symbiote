from common import *
import logging; logging.disable(logging.CRITICAL)

class LLM:
    """Falha em loop para a sessão que pede 'loop'; responde texto para o resto."""
    def complete(self, messages, config=None, tools=None):
        users = [m for m in messages if m.get("role") == "user"]
        text = " ".join(str(m.get("content")) for m in messages)
        if "PLEASE-LOOP" in text:
            return 'trying\n```tool_call\n{"tool": "flaky", "params": {"n": %d}}\n```' % len(messages)
        if "USE-TOOL" in text and not any(m.get("role") != "user" and "pong" in str(m.get("content")) for m in messages[1:]) and len(messages) < 4:
            return '```tool_call\n{"tool": "ping", "params": {}}\n```'
        return "plain answer"

def run(interleave):
    k, d = fresh("r3_" + str(interleave), LLM())
    def flaky(params): raise RuntimeError("boom")
    k.tool_gateway.register_tool("flaky", flaky); k.tool_gateway.register_tool("ping", lambda p: "pong")
    s = k.create_symbiote("s", "assistant")
    k.environment.configure(symbiote_id=s.id, tools=["flaky", "ping"], max_tool_iterations=4)
    a = k.start_session(s.id); b = k.start_session(s.id)
    try: k.message(a.id, "PLEASE-LOOP")
    except Exception as e: print("  (message A levantou", type(e).__name__, ")")
    tr = k._storage.fetch_one("select stop_reason, total_iterations from execution_traces where session_id=?", (a.id,))
    if interleave:
        k.message(b.id, "USE-TOOL")        # outra sessão conversa antes de A fechar
    k.close_session(a.id)
    sc = k._storage.fetch_one("select auto_score, stop_reason from session_scores where session_id=?", (a.id,))
    fm = k._storage.fetch_all("select content from memory_entries where tags_json like '%harness_failure%'")
    print(f"interleave={interleave}: trace persistido={tr} | score de A={sc} | failure memories={len(fm)}")

run(False)
run(True)
