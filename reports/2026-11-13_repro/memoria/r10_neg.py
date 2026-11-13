from common import *
import json, threading, os, sqlite3
import logging; logging.disable(logging.CRITICAL)
from symbiote.skills.store import SkillsStore, SkillError
root = S / "run" / "r10" / "skills"; shutil.rmtree(root.parent, ignore_errors=True)
st = SkillsStore([root]); st.create("ok", "---\nname: ok\n---\nx\n")
print("== traversal ==")
for n in ["../x", "a/b", "..", ".hidden", "/abs", "a\\b", "A", ""]:
    try: st.create(n, "c"); print("  name", repr(n), "ACEITO")
    except SkillError as e: print("  name", repr(n), "rejeitado")
for fp in ["../x", "/etc/passwd", "references/../../x", "SKILL.md", ".skill_meta.json", "references/../SKILL.md", "scripts/a.sh"]:
    try: r = st.write_file("ok", fp, "c"); print("  file_path", repr(fp), "ACEITO ->", r.path.relative_to(root))
    except SkillError as e: print("  file_path", repr(fp), "rejeitado")
# symlink em subdir permitido apontando para fora
out = S / "run" / "r10" / "outside"; out.mkdir()
os.symlink(out, root / "ok" / "assets")
try: r = st.write_file("ok", "assets/p.txt", "c"); print("  via symlink assets -> fora:", "ESCREVEU FORA" if (out / "p.txt").exists() else "ok")
except Exception as e: print("  symlink rejeitado", e)

print("== JSON malformado do LLM ==")
k, d = fresh("r10b"); ev = MockLLMAdapter("Sorry, I can't produce JSON {{{ ["); k.set_evolver_llm(ev)
s = k.create_symbiote("s", "x")
k.environment.configure(symbiote_id=s.id, reflection_mode="llm", skill_review_enabled=True, dream_mode="full")
x = k.start_session(s.id); k.message(x.id, "I always prefer tabs over spaces"); 
print("  close_session:", k.close_session(x.id).status); k._background_review_for(s.id)._last_thread.join(5)
print("  reflection_audit.llm_error:", k._storage.fetch_one("select llm_error from reflection_audit")["llm_error"][:50])
print("  skill_review_audit:", dict(k._storage.fetch_one("select ok, error from skill_review_audit")))
k._storage.execute("insert into execution_traces (id, session_id, symbiote_id, total_iterations, total_tool_calls, total_elapsed_ms, stop_reason, steps_json, created_at) values ('t', ?, ?, 3, 3, 1, 'stagnation', ?, '2026-11-13T00:00:00+00:00')", (x.id, s.id, json.dumps([{"tool_id": "a", "success": False, "error": "e"}])))
for resp in ['{"rules": []}', '["just a string"]', '[{"content": "c", "importance": null}]', 'garbage']:
    ev.default_response = resp
    rep = k.dream(s.id)
    print("  dream mine com", resp[:28].ljust(28), "->", [(p.phase, (p.error or "")[:40]) for p in rep.phases if p.phase == "mine"])

print("== lock do SQLiteAdapter sob 16 threads ==")
errs = []
def w(i):
    try:
        for j in range(300):
            k._storage.execute("insert into messages (id, session_id, role, content, created_at) values (?,?,?,?,?)", (f"{i}-{j}", x.id, "user", "c", "t"))
            k._storage.fetch_all("select * from messages where session_id=? limit 5", (x.id,))
    except Exception as e: errs.append(repr(e))
ts = [threading.Thread(target=w, args=(i,)) for i in range(16)]; [t.start() for t in ts]; [t.join() for t in ts]
print("  erros:", errs[:2], "| linhas:", k._storage.fetch_one("select count(*) c from messages")["c"])
c = sqlite3.connect(":memory:")
print("== filtro de data do evolver/tuner ==", c.execute("select '2026-11-06T23:59:59+00:00' >= datetime('2026-11-13 12:00:00','-7 days'), '2026-11-06T00:00:01+00:00' >= datetime('2026-11-13 12:00:00','-7 days'), '2026-11-05T23:59:59+00:00' >= datetime('2026-11-13 12:00:00','-7 days')").fetchone())
