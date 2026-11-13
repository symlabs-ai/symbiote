from common import *
import json
import logging; logging.disable(logging.CRITICAL)
k, d = fresh("r5e", MockLLMAdapter("ok"))
ev = MockLLMAdapter("[]"); k.set_evolver_llm(ev)
s = k.create_symbiote("s", "assistant")
k.environment.configure(symbiote_id=s.id, skill_review_enabled=True, max_quarantine_skills=2)
for i in range(6):
    ev.default_response = json.dumps([{"action": "create", "name": f"ghost{i}", "content": "playbook without frontmatter"}])
    x = k.start_session(s.id); k.message(x.id, "we fixed it by clearing the cache"); k.close_session(x.id)
    k._background_review_for(s.id)._last_thread.join(5)
rows = k._storage.fetch_all("select applied, skipped from skill_review_audit")
print("max_quarantine_skills=2 | 6 reviews:", [(r["applied"], r["skipped"]) for r in rows])
print("dirs em disco:", sorted(p.name for p in (d / "skills").iterdir()), "| loader.list_skills():", k.skills_loader.list_skills())
rep = k.dream(s.id)
print("dream prune (GC de skills) viu:", [x for p in rep.phases for x in p.details])
