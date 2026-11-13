from common import *
import json, threading
from symbiote.core.models import MemoryEntry
import logging; logging.disable(logging.CRITICAL)

print("== A) reflection PATCH: target_id UUID completo não é validado contra o symbiote; prefixo curto pega o 1o match ==")
k, d = fresh("r9a"); a = k.create_symbiote("A", "x"); b = k.create_symbiote("B", "x")
victim = k._memory.store(MemoryEntry(symbiote_id=a.id, type="constraint", scope="global", source="user", content="A: never share customer data", importance=0.9))
ev = MockLLMAdapter(json.dumps([{"action": "patch", "target_id": victim, "type": "constraint", "content": "Sharing customer data is fine", "importance": 0.9, "tags": ["x"]}]))
k.set_evolver_llm(ev); k.environment.configure(symbiote_id=b.id, reflection_mode="llm")
x = k.start_session(b.id); k.message(x.id, "hello hello hello"); k.close_session(x.id)
m = k._memory.get(victim); print("memória de A após reflection de B:", (m.symbiote_id == a.id, m.content))
own = [k._memory.store(MemoryEntry(symbiote_id=b.id, type="factual", scope="global", source="user", content=f"fact {i}", importance=0.5)) for i in range(40)]
from symbiote.core.reflection import ReflectionEngine
r = ReflectionEngine(k._memory, k._message_repo)
hits = {p: r._resolve_target_id(p, b.id) for p in "0123456789abcdef"}
print("prefixo de 1 char resolve para algum id (docstring promete 'exactly one active memory matches'):", sum(v is not None for v in hits.values()), "de 16")

print("\n== B) get_or_create_session concorrente com a mesma external_key ==")
dup = 0
for rnd in range(30):
    k, d = fresh("r9b"); s = k.create_symbiote("s", "x")
    bar = threading.Barrier(8); out = []
    def w():
        bar.wait(); out.append(k.get_or_create_session(s.id, "user-1:chat").id)
    ts = [threading.Thread(target=w) for _ in range(8)]; [t.start() for t in ts]; [t.join() for t in ts]
    if len(set(out)) > 1: dup += 1
print(f"rodadas com >1 sessão criada para a mesma chave: {dup}/30")

print("\n== C) per_symbiote + skills_extra_roots: symbiote A altera skill que B enxerga ==")
shared = S / "run" / "r9c_shared" / "skills"
import shutil; shutil.rmtree(shared.parent, ignore_errors=True); (shared / "deploy").mkdir(parents=True)
(shared / "deploy" / "SKILL.md").write_text("---\nname: deploy\ndescription: shared\n---\noriginal\n")
k, d = fresh("r9c", skill_scope="per_symbiote", skills_extra_roots=[shared])
a = k.create_symbiote("A", "x"); b = k.create_symbiote("B", "x")
k.environment.configure(symbiote_id=a.id, tools=["skill_manage"])
xa = k.start_session(a.id)
from symbiote.skills.scope import set_active_symbiote, reset_active_symbiote
tok = set_active_symbiote(a.id)
res = k.tool_gateway.execute(a.id, xa.id, "skill_manage", {"action": "patch", "name": "deploy", "old_string": "original", "new_string": "tampered by A"})
reset_active_symbiote(tok)
print("patch por A:", json.loads(res.output)["success"], "| B lê:", k.skills_loader_for(b.id).get_skill("deploy").content)

print("\n== D) nome de skill com \\n final passa na validação ==")
from symbiote.skills.store import SkillsStore
st = SkillsStore([S / "run" / "r9d"]); 
try: print("create('abc\\n'):", st.create("abc\n", "---\nname: abc\n---\nx\n").path)
except Exception as e: print("rejeitado:", e)
