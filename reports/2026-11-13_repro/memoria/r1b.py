from common import *
from symbiote.core.models import MemoryEntry
llm = MockLLMAdapter("ok"); k, d = fresh("r1b", llm); s = k.create_symbiote("s", "assistant")
k._memory.store(MemoryEntry(symbiote_id=s.id, type="preference", scope="global", source="user", content="User prefers the vim editor", importance=0.9))
x = k.start_session(s.id)
for q in ["Which editor do I prefer?", "what do you know about my editor", "vim"]:
    k.message(x.id, q)
    p = "\n".join(str(m.get("content")) for m in llm.calls[-1]["messages"])
    print(f"{q!r:40} -> memória no prompt: {'vim editor' in p}")
