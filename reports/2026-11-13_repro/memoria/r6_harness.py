from common import *
import json
from datetime import UTC, datetime
from uuid import uuid4
import logging; logging.disable(logging.CRITICAL)
DEFAULT = "Use tools carefully.\nCRITICAL: never invent tool names.\nAnswer briefly."
def active(k, sid): 
    r = k.harness_versions.get_active_version(sid, "tool_instructions"); return (r["version"], r["content"][:14]) if r else None

print("== A) rollback reativa 'version-1' (não o parent): versão já revertida volta a ficar ativa ==")
k, d = fresh("r6a"); s = k.create_symbiote("s", "x"); hv = k.harness_versions
hv.create_version(s.id, "tool_instructions", "v1 GOOD text", parent_version=None)
hv.create_version(s.id, "tool_instructions", "v2 BAD text", parent_version=1)
hv.rollback(s.id, "tool_instructions");            print("após rollback de v2:", active(k, s.id))
hv.create_version(s.id, "tool_instructions", "v3 WORSE text", parent_version=1)
hv.rollback(s.id, "tool_instructions");            print("após rollback de v3:", active(k, s.id), "(esperado v1)")

print("\n== B) 1a versão evoluída nunca é revertida (parent=None; baseline do texto default não é medido) ==")
k, d = fresh("r6b"); s = k.create_symbiote("s", "x"); hv = k.harness_versions
hv.create_version(s.id, "tool_instructions", "v1 TERRIBLE text", parent_version=None)
for i in range(60): hv.update_score(s.id, "tool_instructions", 0.0)
row = hv.get_active_version(s.id, "tool_instructions")
print("v1: avg_score=%s session_count=%s -> check_harness_rollback=%s" % (row["avg_score"], row["session_count"], k.check_harness_rollback(s.id, "tool_instructions")))
from symbiote.harness.evolver import HarnessEvolver
print("motivo:", HarnessEvolver(k._storage, hv).check_rollback(s.id, "tool_instructions").reason)

print("\n== C) guardas MIN_FAILED_SESSIONS=5 / MIN_SUCCESSFUL_SESSIONS=3 satisfeitas por 2 sessões (JOIN duplica por trace); sem intervalo mínimo ==")
proposer = MockLLMAdapter("Use tools carefully!!\nCRITICAL: never invent tool names.\nAnswer briefly.")
k, d = fresh("r6c", proposer); s = k.create_symbiote("s", "x")
now = datetime.now(tz=UTC).isoformat()
def sess(score, n_traces, stop):
    sid = k.start_session(s.id).id
    k._storage.execute("insert into session_scores (id, session_id, symbiote_id, auto_score, final_score, stop_reason, computed_at) values (?,?,?,?,?,?,?)",
                       (str(uuid4()), sid, s.id, score, score, stop, now))
    for _ in range(n_traces):   # kernel grava 1 execution_trace por message()
        k._storage.execute("insert into execution_traces (id, session_id, symbiote_id, total_iterations, total_tool_calls, total_elapsed_ms, stop_reason, steps_json, created_at) values (?,?,?,?,?,?,?,?,?)",
                           (str(uuid4()), sid, s.id, 3, 3, 10, stop, "[]", now))
sess(0.1, 5, "max_iterations"); sess(0.9, 3, "end_turn")
print("sessões reais: 1 falha + 1 sucesso")
for i in range(3):
    r = k.evolve_harness(s.id, "tool_instructions", DEFAULT)
    print(f" evolve #{i+1}: success={r.success} new_version={r.new_version} reason={r.reason!r}")
print("versões criadas em sequência (MIN_EVOLUTION_INTERVAL_DAYS=7 nunca é consultado):",
      [(v["version"], v["is_active"], v["session_count"], v["parent_version"]) for v in k.harness_versions.list_versions(s.id, "tool_instructions")])

print("\n== D) ParameterTuner: regra 'too_high' sobrescreve 'too_low' e reduz o limite enquanto 83% batem nele ==")
from symbiote.harness.tuner import ParameterTuner
k, d = fresh("r6d"); s = k.create_symbiote("s", "x")
k.environment.configure(symbiote_id=s.id, max_tool_iterations=10)
def tr(stop, iters):
    k._storage.execute("insert into execution_traces (id, session_id, symbiote_id, total_iterations, total_tool_calls, total_elapsed_ms, stop_reason, steps_json, tool_mode, created_at) values (?,?,?,?,?,?,?,?,?,?)",
                       (str(uuid4()), "x", s.id, iters, iters, 10, stop, "[]", "brief", now))
for _ in range(25): tr("max_iterations", 10)
for _ in range(5): tr("end_turn", 1)
t = ParameterTuner(k._storage); res = t.analyze(s.id)
print("tier", res.tier, "| ajuste:", res.adjustments, "|", res.reasons)
t.apply(res, k.environment); print("max_tool_iterations aplicado:", k.environment.get_max_tool_iterations(s.id))
