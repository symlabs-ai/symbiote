from common import *
llm = FakeLLM(lambda i, m, c, t: f"resposta {i}")
k = mk_kernel(llm)
sym = k.create_symbiote("a", "assistant")
s = k.start_session(sym.id)
k.message(s.id, "Meu nome e Rodrigo e meu codigo secreto e ZX-991.")
k.message(s.id, "Qual e meu codigo secreto?")
k.message(s.id, "codigo secreto ZX?")
for i, c in enumerate(llm.calls):
    blob = "\n".join(str(m["content"]) for m in c["messages"])
    print(f"call {i}: n_msgs={len(c['messages'])} roles={[m['role'] for m in c['messages']]} 'ZX-991' in prompt={'ZX-991' in blob} 'resposta 0' in prompt={'resposta 0' in blob}")
print("persistidas na sessao:", [(m.role, m.content[:30]) for m in reversed(k._sessions.get_messages(s.id, limit=20))])
