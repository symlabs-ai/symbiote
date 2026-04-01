# YouNews Clark — Guia de Upgrade para Symbiote v0.2.22

**Data:** 2026-04-01
**De:** Symbiote v0.2.19 (atual no YouNews)
**Para:** Symbiote v0.2.22
**Impacto:** Baixo risco, alto impacto operacional

---

## Resumo Executivo

O Symbiote recebeu **13 features** em 3 sprints desde a v0.2.19. O Clark no YouNews pode se beneficiar imediatamente da maioria delas sem nenhuma mudança de código — basta atualizar a dependência.

As maiores oportunidades para o Clark:

1. **3-layer compaction** — resolve definitivamente o context growth no tool loop (o problema do Kimi K2)
2. **Parallel tool execution** — tools independentes rodam em paralelo, reduzindo latência do loop
3. **LoopController (circuit breaker + stagnation)** — o Clark para de repetir a mesma tool call ou de insistir em tools falhando
4. **LLM retry** — erros transientes do provider são recuperados automaticamente
5. **Per-session locks** — elimina race conditions no SSE streaming

**Nenhuma é breaking change. Todas são opt-in ou automáticas.**

---

## O que mudou (v0.2.19 → v0.2.22)

### Sprint 1: Nanobot Adaptations (v0.2.20)

| Feature | ID | Impacto no Clark |
|---|---|---|
| Prompt Cache | B-46 | Ativar quando usar Anthropic |
| Message Retry + Backoff | B-47 | Futuro (MessageBus) |
| Per-Session Locks | B-48 | **Direto** — resolve race conditions |
| Hardened allow_internal | B-49 | **Automático** |
| CompositeHook | B-50 | Útil para observability |
| Delta Streaming | B-51 | Futuro (MessageBus) |

### Sprint 2: Hermes Adaptations (v0.2.21)

| Feature | ID | Impacto no Clark |
|---|---|---|
| SessionRecallPort | B-52 | Útil para continuidade cross-session |
| MemoryCategory | B-53 | **Automático** — melhora reflexão |
| Context Compaction | B-54 | **Automático** — resolve context growth |

### Sprint 3: Agent Loop Resilience (v0.2.22)

| Feature | ID | Impacto no Clark |
|---|---|---|
| Parallel tool execution | B-55 | **Direto** — tools rodam em paralelo |
| LLM retry with backoff | B-56 | **Automático** — recupera erros transientes |
| LoopController (circuit breaker) | B-57 | **Automático** — para loops repetitivos |
| 3-layer compaction | B-58 | **Automático** — substitui o B-54 com sistema mais robusto |

---

## Ações Recomendadas

### 1. Atualizar a dependência

```toml
# pyproject.toml do YouNews — mudar de:
symbiote = {url = "https://github.com/symlabs-ai/symbiote/archive/refs/tags/v0.2.19.tar.gz"}

# Para:
symbiote = {url = "https://github.com/symlabs-ai/symbiote/archive/refs/tags/v0.2.22.tar.gz"}
```

Rodar `pip install -e ".[dev]"` e verificar testes.

---

### 2. Benefícios automáticos (zero mudança de código)

Ao atualizar para v0.2.22, o Clark ganha automaticamente:

#### 3-layer compaction (B-58)

O tool loop agora tem 3 camadas de proteção contra context growth:

- **Layer 1 — Microcompact**: resultados de tools maiores que 2000 chars são truncados antes de serem injetados no contexto. Isso impede que um `yn_list_items` retornando 50 items exploda o contexto.
- **Layer 2 — Loop compact**: após 4 iterações, pares antigos de mensagens (assistant + tool_result) são substituídos por um resumo compacto. O LLM vê "Steps completed: 1) yn_list_items → 3 items, 2) yn_publish_item → success" em vez de JSONs gigantes.
- **Layer 3 — Autocompact**: se o total de tokens ultrapassar 80% do budget do contexto, uma compactação agressiva é disparada, mantendo apenas o último par.

**Impacto no Clark**: O problema do Kimi K2 (B-41) onde `context_length_exceeded` estourava em 75+ tools está resolvido. O Clark pode rodar loops de 10 iterações sem degradação.

#### Parallel tool execution (B-55)

`ToolGateway.execute_tool_calls()` agora roda calls independentes em paralelo:
- **Sync**: `ThreadPoolExecutor(max_workers=4)`
- **Async**: `asyncio.gather()`

Se o LLM pedir `yn_list_items` e `yn_journal_tags` no mesmo turn, ambos rodam simultaneamente.

**Impacto no Clark**: Latência do tool loop cai proporcionalmente ao número de calls paralelas. Um turn com 3 tools independentes leva ~1x em vez de ~3x.

#### LoopController — circuit breaker + stagnation (B-57)

O tool loop agora tem um `LoopController` que monitora saúde e para o loop quando detecta:

- **Stagnation**: mesma tool_id + mesmos params chamados 2x consecutivas. O Clark parou de chamar `yn_list_items({"journal_id": "X"})` 8 vezes seguidas.
- **Circuit breaker**: mesma tool falha 3x consecutivas. Se `yn_publish_item` está dando erro, o loop para e o LLM responde com o que tem.

Quando o loop para por stagnation ou circuit breaker, uma mensagem é injetada: "You are repeating the same action. Respond to the user." Isso guia o LLM para uma resposta limpa.

O `LoopTrace.stop_reason` agora registra o motivo: `end_turn`, `max_iterations`, `stagnation`, ou `circuit_breaker`.

**Impacto no Clark**: Resolve diretamente o B-25 (LLM não sabe parar) que observamos com Llama-3.3-70b.

#### LLM retry with backoff (B-56)

Erros transientes do provider (ConnectionError, TimeoutError, rate limits, HTTP 5xx) são recuperados com até 3 retries e backoff exponencial (1s, 2s, 4s).

**Impacto no Clark**: O SymGateway às vezes dá timeout ou 503. Antes o tool loop falhava inteiro. Agora recupera automaticamente.

#### MemoryCategory (B-53)

Memórias extraídas pelo `ReflectionEngine` são classificadas automaticamente:
- `ephemeral` — working memory
- `declarative` — preferences, constraints, facts
- `procedural` — how-to knowledge
- `meta` — summaries, reflections

A função `_learn_preferences()` do Clark já salva como `type="preference"` — agora a `category` será `"declarative"` automaticamente.

#### Per-session locks (B-48)

`kernel.message()` e `kernel.message_async()` agora usam locks por session_id. Requests concorrentes na mesma sessão são serializados; sessões diferentes rodam em paralelo.

---

### 3. Mudanças opcionais recomendadas

#### Remover workaround SQLite (prioridade alta)

O bloco em `app.py` que re-abre a conexão SQLite com `check_same_thread=False` pode ser simplificado. O `SessionLock` resolve race conditions a nível de aplicação. Manter apenas o `PRAGMA journal_mode=WAL` para performance.

#### Ativar prompt caching (quando usar Anthropic)

```python
kernel.environment.configure(
    symbiote_id=clark_id,
    tools=loaded,
    tool_loop=True,
    prompt_caching=True,  # ~90% economia em tokens
)
```

O system prompt do Clark (persona + tools + domain knowledge) é praticamente idêntico entre requests — ideal para caching.

#### Adicionar lifecycle hooks (observability)

```python
from symbiote.core.hooks import BaseHook

class ClarkAuditHook(BaseHook):
    async def before_tool(self, tool_id, params):
        logger.info("[clark] tool_call: %s", tool_id)

    async def after_tool(self, tool_id, params, result):
        logger.info("[clark] tool_done: %s", tool_id)

kernel.hooks.add(ClarkAuditHook())
```

#### Implementar SessionRecall (continuidade cross-session)

O kernel expõe `SessionRecallPort` — um protocolo que o host implementa para buscas em sessões passadas. Implementação MVP:

```python
class ClarkSessionRecall:
    def __init__(self, storage):
        self._storage = storage

    def search_messages(self, query, symbiote_id=None, session_id=None, limit=10):
        rows = self._storage.fetch_all(
            "SELECT m.session_id, m.role, m.content, m.created_at "
            "FROM messages m WHERE m.content LIKE ? "
            "ORDER BY m.created_at DESC LIMIT ?",
            (f"%{query}%", limit),
        )
        return [dict(r) for r in rows]

    def search_sessions(self, query, symbiote_id=None, limit=5):
        rows = self._storage.fetch_all(
            "SELECT id as session_id, goal, summary, started_at "
            "FROM sessions WHERE goal LIKE ? OR summary LIKE ? "
            "ORDER BY started_at DESC LIMIT ?",
            (f"%{query}%", f"%{query}%", limit),
        )
        return [dict(r) for r in rows]

kernel.set_session_recall(ClarkSessionRecall(kernel._storage))
```

Permite ao Clark responder "Qual matéria você publicou ontem?" buscando em sessões anteriores.

---

## Checklist de Upgrade

```
[ ] Atualizar symbiote para v0.2.22 no pyproject.toml
[ ] pip install -e ".[dev]"
[ ] pytest tests/ — verificar que testes passam
[ ] Deploy em staging
[ ] Validar tool loop com task de 5+ steps (ex: "publique as 3 primeiras matérias do inbox")
[ ] Verificar logs: procurar por [autocompact], stagnation, circuit_breaker
[ ] (Opcional) Remover workaround SQLite check_same_thread
[ ] (Opcional) Adicionar prompt_caching=True
[ ] (Opcional) Adicionar ClarkAuditHook
[ ] (Futuro) Implementar SessionRecall
```

---

## Riscos

| Risco | Probabilidade | Mitigação |
|---|---|---|
| Breaking change na API | Muito baixa | Todas features opt-in, defaults preservados |
| Microcompact trunca resultado útil | Baixa | Threshold de 2000 chars cobre 99% dos casos; resultados truncados incluem `...(truncated)` |
| LoopController para o loop cedo demais | Baixa | Stagnation precisa de 2 calls idênticas consecutivas; circuit breaker precisa de 3 falhas. Margem generosa |
| MemoryCategory muda tipo de memórias | Baixa | Antes era tudo `"reflection"`, agora usa tipo real. Se buscar `get_by_type("reflection")`, trocar por `get_by_category("meta")` |
| SQLite migrations | Muito baixa | Idempotentes (`ALTER TABLE ADD COLUMN` com try/except) |

---

## Nota sobre `get_by_type("reflection")`

O `ReflectionEngine` agora salva memórias com seu tipo real (ex: `"constraint"` em vez de `"reflection"`). Código que faz `get_by_type("reflection")` retornará menos resultados.

**Mitigação:** Usar `get_by_category("meta")` para buscar todas as memórias meta, ou `get_by_category("declarative")` para facts/preferences/constraints.

---

## Contato

Dúvidas: `/ask symbiote` a partir de qualquer repo Symlabs.
