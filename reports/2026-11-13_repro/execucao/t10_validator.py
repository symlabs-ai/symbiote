"""Response validator false positives: valid answers are discarded and replaced by the apology fallback."""
import logging
from common import *
from symbiote.runners.response_validator import is_valid_response
logging.disable(logging.CRITICAL)
samples = {
 "citacoes": "Segundo as fontes:\n[1] Relatorio anual 2025\n[2] Nota tecnica 14",
 "link markdown no inicio da linha": "Veja:\n[Documentacao](https://example.com/docs)",
 "checklist": "Tarefas:\n[x] criar conta\n[ ] validar email",
 "exemplo JSON pedido pelo usuario": "Claro, um exemplo de payload:\n```json\n{\n  \"name\": \"Ana\"\n}\n```",
 "codigo python": "Use:\n```python\nitems = [\n    1, 2]\n```",
 "explicando o formato": 'O campo "tool": "x" identifica a ferramenta.',
}
for name, text in samples.items():
    print(f"is_valid_response({name}) = {is_valid_response(text)}")

answer = samples["citacoes"]
llm = FakeLLM(lambda i, m, c, t: answer)       # model keeps giving the same (legit) answer
k = mk_kernel(llm)
sym = k.create_symbiote("a", "assistant")
s = k.start_session(sym.id)
out = k.message(s.id, "quais as fontes?")
print("llm_calls:", len(llm.calls), "| resposta entregue:", repr(out))
print("instrucao injetada:", llm.calls[1]["messages"][-1]["content"][:90])
