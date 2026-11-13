from common import *
import time
import logging; logging.disable(logging.CRITICAL)
def cnt(k, sql, *p): return k._storage.fetch_one(sql, p)["c"]
def run(touch_a_first):
    k, d = fresh("r4d%d" % touch_a_first)
    a = k.create_symbiote("a", "x"); b = k.create_symbiote("b", "x")
    k.environment.configure(symbiote_id=a.id, dream_mode="light", dream_min_sessions=50)
    k.environment.configure(symbiote_id=b.id, dream_mode="light", dream_min_sessions=2)
    if touch_a_first:
        x = k.start_session(a.id); k.message(x.id, "hi"); k.close_session(x.id)   # A fecha 1 sessão primeiro
    per_close = []
    for i in range(4):
        x = k.start_session(b.id); k.message(x.id, "hi"); k.close_session(x.id); time.sleep(0.2)
        per_close.append(cnt(k, "select count(*) c from dream_reports where symbiote_id=?", b.id))
    print(f"A usou o engine antes={bool(touch_a_first)}: engine._min_sessions={k._dream_engine._min_sessions} | dream_reports de B após cada close (min_sessions de B=2): {per_close}")
run(0); run(1)
