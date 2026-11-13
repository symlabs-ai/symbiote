"""ProcessEngine: step index lives only in memory; work() cannot reach ProcessRunner with 'intent: task'."""
import logging
from common import *
from symbiote.process.engine import ProcessEngine
logging.disable(logging.CRITICAL)
k = mk_kernel(None)
sym = k.create_symbiote("a", "assistant")
s = k.start_session(sym.id)

eng = ProcessEngine(k._storage)
inst = eng.start(s.id, "research_task")           # define_scope -> gather -> analyze -> report
eng.advance(inst.id); eng.advance(inst.id)
print("antes do restart: current_step =", eng.get_instance(inst.id).current_step)
eng2 = ProcessEngine(k._storage)                  # process restart (or eng.invalidate_cache(inst.id))
seq = []
i2 = eng2.get_instance(inst.id)
while i2.state == "running" and len(seq) < 10:
    i2 = eng2.advance(inst.id); seq.append(i2.current_step)
print("apos restart, advance() percorre:", seq, "| logs:", [l["step"] for l in i2.logs])

eng3 = ProcessEngine(k._storage)
inst = eng3.start(s.id, "research_task"); eng3.advance(inst.id); eng3.advance(inst.id)
eng3.invalidate_cache(inst.id)
print("apos invalidate_cache: de 'analyze' advance() vai para ->", eng3.advance(inst.id).current_step)

print("work('research_task') ->", {k_: v for k_, v in k.capabilities.work(sym.id, s.id, "research_task").items() if k_ in ("success", "error")})
print("work('research_task: mapear concorrentes') ->", {k_: v for k_, v in k.capabilities.work(sym.id, s.id, "research_task: mapear concorrentes").items() if k_ in ("success", "error", "runner_type")})
print("caches nunca esvaziados (instancias concluidas):", len(eng2._instances), len(eng2._instance_step_index))
