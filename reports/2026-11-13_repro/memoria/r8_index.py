from common import *
import time, sqlite3, uuid
from datetime import UTC, datetime, timedelta
import logging; logging.disable(logging.CRITICAL)
llm = MockLLMAdapter("ok")
k, d = fresh("r8", llm)
s = k.create_symbiote("s", "assistant")
sess = k.start_session(s.id)
c = k._storage._conn
print("índices existentes:", sorted(r[0] for r in c.execute("select name from sqlite_master where type='index' and name not like 'sqlite_%'")))
for label, sql in [
  ("mensagens por sessão (get_messages / COUNT em toda message())", "SELECT COUNT(*) FROM messages WHERE session_id = 'x'"),
  ("get_relevant", "SELECT * FROM memory_entries WHERE is_active = 1 AND content LIKE '%x%' ORDER BY importance DESC, last_used_at DESC LIMIT 5"),
  ("get_by_type", "SELECT * FROM memory_entries WHERE is_active = 1 AND symbiote_id = 'x' AND type = 'y' ORDER BY importance DESC LIMIT 5"),
  ("should_dream", "SELECT COUNT(*) FROM sessions WHERE symbiote_id = 'x' AND status = 'closed' AND ended_at > 'y'"),
  ("evolver JOIN", "SELECT ss.* FROM session_scores ss LEFT JOIN execution_traces et ON ss.session_id = et.session_id WHERE ss.symbiote_id = 'x' AND ss.final_score < 0.5"),
]:
    print(f"  {label}: ", [r[3] for r in c.execute("EXPLAIN QUERY PLAN " + sql)])

def t_message():
    t = time.perf_counter(); k.message(sess.id, "hello"); return (time.perf_counter() - t) * 1000
base = min(t_message() for _ in range(5))
# popula 400k mensagens de outras sessões (~1 KB cada) direto via SQL
now = datetime.now(tz=UTC)
other = [k.start_session(s.id).id for _ in range(200)]
rows = ((str(uuid.uuid4()), other[i % 200], "user", "x" * 1000, (now - timedelta(seconds=i)).isoformat()) for i in range(400_000))
with k._storage._lock:
    c.executemany("INSERT INTO messages (id, session_id, role, content, created_at) VALUES (?,?,?,?,?)", rows); c.commit()
big = min(t_message() for _ in range(5))
print(f"kernel.message() com LLM mock: {base:.1f} ms (tabela vazia) -> {big:.1f} ms (400k mensagens de outras sessões)")
t = time.perf_counter(); k.close_session(sess.id); print(f"close_session: {(time.perf_counter()-t)*1000:.1f} ms")
print("traces por mensagem / retenção: execution_traces =", c.execute("select count(*) from execution_traces").fetchone()[0], "linhas p/",
      c.execute("select count(*) from messages where session_id=?", (sess.id,)).fetchone()[0], "mensagens da sessão (modo brief só grava trace quando há tool call)")
