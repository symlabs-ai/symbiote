from common import *
from symbiote.core.models import MemoryEntry
llm = MockLLMAdapter("ok")
k, d = fresh("r1", llm)
a = k.create_symbiote("tenant-A", "assistant")
b = k.create_symbiote("tenant-B", "assistant")
mid = k._memory.store(MemoryEntry(symbiote_id=a.id, type="factual", scope="global", source="user",
    content="A's API key for billing is sk-AAAA-SECRET", importance=0.9))
sb = k.start_session(b.id)
k.message(sb.id, "billing")
prompt = "\n".join(str(m.get("content")) for m in llm.calls[-1]["messages"])
print("1) prompt de B (msg 'billing') contém segredo de A:", "sk-AAAA-SECRET" in prompt)
k.message(sb.id, "%")
prompt = "\n".join(str(m.get("content")) for m in llm.calls[-1]["messages"])
print("2) prompt de B (msg '%') contém segredo de A:", "sk-AAAA-SECRET" in prompt)
k.environment.configure(symbiote_id=b.id, tools=["search_memories"], context_mode="on_demand")
r = k.tool_gateway.execute(b.id, sb.id, "search_memories", {"query": "API key"})
print("3) tool search_memories executada por B:", r.success, r.output)
print("4) MemoryStore.search('API'):", [m.content for m in k._memory.search("API")])
# recall natural não funciona: a mensagem inteira precisa ser substring da memória
k._memory.store(MemoryEntry(symbiote_id=b.id, type="preference", scope="global", source="user",
    content="User prefers the vim editor", importance=0.9))
print("5) get_relevant('Which editor do I prefer?') ->", [m.content for m in k._memory.get_relevant("Which editor do I prefer?", sb.id)])
print("   get_relevant('vim') ->", [m.content for m in k._memory.get_relevant("vim", sb.id)])
from symbiote.memory.recall import SemanticRecallProvider
print("   SemanticRecallProvider.recall (código morto, não ligado ao kernel) ->",
      [m.content for m in SemanticRecallProvider(k._storage).recall("Which editor do I prefer?")])
