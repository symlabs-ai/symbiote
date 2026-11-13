from common import *
import json, time, sys, threading
class Slow:
    def complete(self, messages, config=None, tools=None):
        time.sleep(0.5)
        return json.dumps([{"action": "create", "name": "cache-fix", "content": "---\nname: cache-fix\ndescription: d\n---\nbody\n"}])
k, d = fresh("r7", MockLLMAdapter("ok"))
k.set_evolver_llm(Slow())
s = k.create_symbiote("s", "assistant")
k.environment.configure(symbiote_id=s.id, skill_review_enabled=True, dream_mode="full", dream_min_sessions=1)
x = k.start_session(s.id); k.message(x.id, "we fixed the deploy by clearing the cache"); k.close_session(x.id)
print("threads vivas no shutdown:", sorted(t.name for t in threading.enumerate() if t is not threading.main_thread()))
k.shutdown()
if "--wait" in sys.argv:
    time.sleep(1.5)
    print("threads vivas 1.5s após shutdown:", sorted(t.name for t in threading.enumerate() if t is not threading.main_thread()))
