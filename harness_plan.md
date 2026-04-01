# Harness Evolution Plan — Symbiote

> Documento vivo de planejamento para evolução do harness agêntico do Symbiote.
> Criado: 2026-04-01
> Última atualização: 2026-04-01
> Status: planejamento ativo
> Referências: `~/dev/kb/engenharia/meta_harness.md` (análise completa), `kb/meta-harness-analysis.md` (resumo no repo)

---

## Contexto

O Symbiote é um kernel embeddable para agentes LLM persistentes. No vocabulário do paper Meta-Harness (Stanford/CMU, 2026), o Symbiote **é** um harness — o código que determina o que armazenar, recuperar e apresentar ao modelo.

O paper demonstra que mudar apenas o harness (sem mudar o modelo) produz **6x de diferença** no mesmo benchmark. A implicação: otimizar o harness automaticamente é tão ou mais valioso que trocar de modelo.

### O que já temos (base de partida)

Implementado no sprint de resiliência (v0.2.22):

| Componente | O que faz | Arquivo |
|---|---|---|
| **LoopController** | Detecta stagnation (2x mesma call) e circuit breaker (3 falhas). Injeta stop message | `runners/loop_control.py` |
| **LoopTrace** | Registra cada iteração: tool_id, params, success, error, elapsed_ms, stop_reason | `runners/base.py` |
| **LLM Retry** | 3 retries com backoff 1s/2s/4s em erros transientes | `runners/chat.py` |
| **Parallel Tools** | asyncio.gather (async) + ThreadPoolExecutor (sync, max 4 workers) | `environment/tools.py` |
| **3-Layer Compaction** | L1: microcompact (trunca >2000 chars), L2: loop compact (resume pares antigos), L3: autocompact (80% budget) | `runners/chat.py` |

Implementado anteriormente (v0.2.20-0.2.21):

| Componente | O que faz |
|---|---|
| **SessionRecallPort** | Protocol para busca host-provided em sessões passadas |
| **MemoryCategory** | Auto-classificação (ephemeral, declarative, procedural, meta) |
| **MemoryConsolidator** | Sumarização via LLM quando working memory excede threshold |
| **CompositeHook** | before/after tool + before/after turn, error isolation |
| **PolicyGate** | Deny-by-default, audit log de toda tool call |

### O que falta (a tese)

O harness hoje é **estático** — mesmas regras, mesmos parâmetros, mesmos prompts para todos os symbiotas, para sempre. O Meta-Harness mostra que harnesses devem ser **evolvable**. A evolução precisa de:

1. **Feedback signal** — medir se o harness está funcionando
2. **Dados persistentes** — traces e scores para análise
3. **Mecanismo de evolução** — algo que use os dados para propor melhorias

---

## Princípios de Design

### 1. Feedback signal composto (3 camadas)

O feedback NÃO precisa vir do usuário. Já temos sinais automáticos:

```
┌─────────────────────────────────────────────────────────┐
│ Sinal 1: "Conseguiu ou desistiu?"                       │
│ ─────────────────────────────────                       │
│ stop_reason     │ Score                                 │
│ end_turn        │ 1.0  — LLM completou naturalmente     │
│ None (sem loop) │ 0.8  — resposta direta, ok            │
│ stagnation      │ 0.2  — repetiu mesma ação             │
│ circuit_breaker │ 0.1  — tool quebrou 3x                │
│ max_iterations  │ 0.0  — esgotou limite                 │
├─────────────────────────────────────────────────────────┤
│ Sinal 2: "Funcionou de primeira?"                       │
│ ─────────────────────────────────                       │
│ 1 iter + end_turn     │ 1.0  — primeira tentativa       │
│ 2-3 iter + end_turn   │ 0.7  — precisou ajustar         │
│ 5+ iter + end_turn    │ 0.4  — custoso mas completou    │
│ Qualquer + falha      │ 0.0  — não funcionou            │
├─────────────────────────────────────────────────────────┤
│ Sinal 3: "Usuário qualificou?" (opcional, via host)     │
│ ─────────────────────────────────                       │
│ Thumbs up / ação positiva │ 1.0                         │
│ Repetiu pergunta          │ 0.2                         │
│ Thumbs down               │ 0.0                         │
│                                                         │
│ Composição:                                             │
│ final = auto_score * 0.6 + user_score * 0.4             │
│ (se sem user_score: final = auto_score)                 │
└─────────────────────────────────────────────────────────┘
```

**Os sinais 1 e 2 são 100% automáticos e já estão no código.** O sinal 3 é bônus.

### 2. "Let the model decide what it needs"

Princípio do Meta-Harness que Berman destaca: em vez de pre-empacotar contexto monolítico, dar ao modelo acesso adaptativo. Já fazemos isso com tools (index mode + semantic mode). O próximo passo é fazer o mesmo com memories e knowledge.

### 3. Evolução em 3 níveis (menor risco primeiro)

```
Nível 1: Parameter Tuning    — zero LLM, SQL puro, zero risco
Nível 2: Prompt Evolution     — LLM barato offline, rollback automático
Nível 3: Structural Evolution — coding agent, sandbox, futuro distante
```

### 4. Rollback sempre

Toda mudança automática é versionada e revertível. Se a versão nova tem score pior após N sessões, reverte automaticamente. O default hardcoded é sempre o fallback.

---

## O que é "evolvable" no harness

### Categoria A — Textos (maior impacto, mais fácil de evoluir)

| Texto | Onde vive | Impacto |
|---|---|---|
| `_TOOL_INSTRUCTIONS` | `runners/chat.py:44-66` | Alto — regras de comportamento com tools |
| `_INDEX_INSTRUCTIONS` | `runners/chat.py:68-71` | Médio — instrução para index mode |
| System prompt structure | `_build_system()` L682+ | Alto — ordem e formatação do contexto |
| Compaction summary format | `_compact_loop_messages()` | Médio — como o resumo é apresentado |
| Injection messages | `loop_control.py:69-82` | Médio — como o LLM é instruído a parar |
| Tool result formatting | `_format_tool_results()` | Baixo — consistência é mais importante que otimização |

### Categoria B — Parâmetros numéricos

| Parâmetro | Valor atual | Onde | Evolvable? |
|---|---|---|---|
| `_MAX_TOOL_ITERATIONS` | 10 | `chat.py:33` | Sim — calibrar por symbiote |
| `_COMPACTION_THRESHOLD` | 4 pairs | `chat.py:34` | Sim — depende do avg iterations |
| `_MICROCOMPACT_MAX_CHARS` | 2000 | `chat.py:37` | Sim — depende do tamanho dos tool results |
| `_AUTOCOMPACT_THRESHOLD` | 0.80 | `chat.py:38` | Sim — depende do context budget |
| `_MEMORIES_SHARE` | 0.40 | `context.py:49` | Sim — depende do uso de memories vs knowledge |
| `_KNOWLEDGE_SHARE` | 0.25 | `context.py:50` | Sim — idem |
| `context_budget` | 4000/16000 | `context.py:64`, `chat.py:39` | Já configurável por host |
| Circuit breaker threshold | 3 | `loop_control.py` | Talvez — 3 é conservador |
| Stagnation threshold | 2 | `loop_control.py` | Provavelmente não — 2 é bom |

### Categoria C — Decisões estruturais (futuro)

| Decisão | Estado atual | Alternativa |
|---|---|---|
| Context pre-packed vs on-demand | Pre-packed (ContextAssembler injeta) | On-demand (memories/knowledge como tools) |
| Ranking de memórias | `LIKE %query%` + importance DESC | BM25, TF-IDF, embeddings via SessionRecallPort |
| Ordem das seções no prompt | Persona → Tools → Context → Memories → Knowledge | Pode depender do task type |

---

## Horizonte de Implementação

### Fase 1 — Fundações (sprint imediato)

> Objetivo: ter o feedback signal funcionando e dados fluindo. Sem mágica, sem LLM extra.
> Estimativa: 1-2 dias de implementação

#### H-01: SessionScore (B-60)
**Prioridade: CRÍTICA — desbloqueia tudo**

Modelo `SessionScore` que computa score 0.0-1.0 automaticamente a partir do `LoopTrace`.

```python
# core/scoring.py (novo)
def compute_auto_score(trace: LoopTrace | None) -> float:
    if trace is None:
        return 0.8  # sem loop = resposta direta

    # Base score pelo stop_reason
    reason_scores = {
        "end_turn": 1.0,
        "stagnation": 0.2,
        "circuit_breaker": 0.1,
        "max_iterations": 0.0,
    }
    base = reason_scores.get(trace.stop_reason or "end_turn", 0.5)

    # Penalizar por iterations (só se end_turn)
    if trace.stop_reason == "end_turn" and trace.total_iterations > 0:
        if trace.total_iterations <= 2:
            iter_factor = 1.0
        elif trace.total_iterations <= 4:
            iter_factor = 0.7
        else:
            iter_factor = 0.4
        base *= iter_factor

    # Penalizar por tool failures
    if trace.steps:
        failure_rate = sum(1 for s in trace.steps if not s.success) / len(trace.steps)
        base *= (1 - failure_rate * 0.3)

    return round(base, 2)
```

**Persistência**: tabela `session_scores`, INSERT em `kernel.close_session()`.

**Observação importante**: precisamos guardar o `RunResult` (ou pelo menos o `loop_trace`) em algum lugar acessível no `close_session()`. Hoje o `RunResult` é retornado ao caller e não persiste. Opções:
- Guardar `last_loop_trace` no `SessionManager` ou no kernel como atributo temporário
- Ou: calcular o score no `ChatRunner.run()` e retornar no `RunResult` (campo novo `auto_score`)
- Ou: fazer B-66 (LoopTrace persistence) junto — a tabela `execution_traces` serve de fonte

**Decisão**: fazer H-01 e H-05 (LoopTrace persistence) juntos, pois o score precisa do trace.

#### H-02: FeedbackPort (B-61)
**Prioridade: alta**

Protocol para o host reportar feedback. Simples — um método que atualiza o score.

```python
# core/ports.py
class FeedbackPort(Protocol):
    def report(self, session_id: str, score: float, source: str) -> None: ...
```

**Implementação inline no kernel**: `kernel.report_feedback(session_id, score, source)` → UPDATE na tabela `session_scores`.

**API**: `POST /sessions/{id}/feedback {"score": 0.9, "source": "user_click"}`

**Observação**: o host pode reportar feedback muito depois do close_session. O score deve ser recalculável: `final = auto * 0.6 + user * 0.4`.

#### H-03: MemoryEntry de falha determinística (B-62)
**Prioridade: alta — valor imediato, zero custo**

Quando `stop_reason != "end_turn"`, gerar `MemoryEntry(type="procedural")` automaticamente.

```python
# No kernel.close_session(), após reflection:
if loop_trace and loop_trace.stop_reason not in ("end_turn", None):
    # Gerar conteúdo baseado no stop_reason
    if loop_trace.stop_reason == "circuit_breaker":
        failed_tool = _find_breaker_tool(loop_trace)
        content = f"Tool '{failed_tool}' falhou múltiplas vezes consecutivas. Verificar pré-condições antes de chamar."
    elif loop_trace.stop_reason == "stagnation":
        last_tool = loop_trace.steps[-1].tool_id if loop_trace.steps else "unknown"
        content = f"Loop estagnou chamando '{last_tool}' repetidamente. Verificar se task já completou."
    elif loop_trace.stop_reason == "max_iterations":
        top_tools = _top_tools(loop_trace, n=3)
        content = f"Sessão esgotou {loop_trace.total_iterations} iterações. Tools mais usadas: {top_tools}. Considerar decompor em passos menores."

    memory_store.store(MemoryEntry(
        symbiote_id=symbiote_id,
        session_id=session_id,
        type="procedural",
        scope="global",
        content=content,
        importance=0.7,
        source="system",
        tags=["harness_failure", loop_trace.stop_reason],
    ))
```

**Observação**: esse MemoryEntry será encontrado pelo `get_relevant()` em sessões futuras quando o user_input mencionar a mesma tool ou task. Não é perfeito (LIKE %query% é bruto), mas é melhor que nada. Melhoria futura: ranking semântico via SessionRecallPort.

#### H-04: Context splits configuráveis (B-63)
**Prioridade: média**

Expor `memory_share` e `knowledge_share` no EnvironmentConfig.

```sql
ALTER TABLE env_configs ADD COLUMN memory_share REAL DEFAULT 0.40;
ALTER TABLE env_configs ADD COLUMN knowledge_share REAL DEFAULT 0.25;
```

O `ContextAssembler._trim_to_budget()` lê os valores do EnvironmentConfig em vez de usar constantes.

**Observação**: se `memory_share + knowledge_share > 1.0`, normalizar. Se host não configura, usa defaults atuais (backward compat).

#### H-05: LoopTrace persistence (B-66)
**Prioridade: alta — pré-requisito para scoring e parameter tuning**

```sql
CREATE TABLE execution_traces (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    symbiote_id TEXT NOT NULL,
    total_iterations INTEGER,
    total_tool_calls INTEGER,
    total_elapsed_ms INTEGER,
    stop_reason TEXT,
    steps_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
CREATE INDEX idx_traces_symbiote ON execution_traces(symbiote_id, created_at);
```

**Implementação**: persist no `kernel.close_session()` (ou no `kernel.message()` após o `run()`).

**Observação sobre o fluxo**: hoje `kernel.message()` retorna o response text (ou dict com tool_results). O `LoopTrace` está dentro do `RunResult` que o `CapabilitySurface.chat()` recebe. Precisamos propagar o trace até o kernel para persist. Opções:
1. `CapabilitySurface.chat()` retorna `(response, trace)` — breaking change na API
2. Kernel mantém um `_last_trace: LoopTrace | None` que é setado após cada `chat()` — simples mas stateful
3. `ChatRunner` persiste diretamente (recebe storage como dep) — viola hexagonal

**Decisão recomendada**: opção 2. O kernel já é stateful (mantém sessions, working memory). Um `_last_trace` que vive entre `message()` e `close_session()` é aceitável. Limpar no `close_session()`.

---

### Fase 2 — Evolução Automática (quando Fase 1 tiver ~200+ sessões com scores)

> Objetivo: o harness começa a se calibrar sozinho.
> Pré-requisito: Fase 1 em produção com dados acumulados.

#### H-06: harness_versions table (B-64)

Versionamento de textos evolvable por symbiote.

```sql
CREATE TABLE harness_versions (
    id TEXT PRIMARY KEY,
    symbiote_id TEXT NOT NULL,
    component TEXT NOT NULL,       -- "tool_instructions", "compaction_format", etc.
    version INTEGER NOT NULL,
    content TEXT NOT NULL,          -- o texto novo
    avg_score REAL DEFAULT 0.0,
    session_count INTEGER DEFAULT 0,
    is_active INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    parent_version INTEGER,         -- para rollback chain
    UNIQUE(symbiote_id, component, version)
);
```

`ChatRunner._build_system()` consulta versão ativa: `SELECT content FROM harness_versions WHERE symbiote_id=? AND component='tool_instructions' AND is_active=1 ORDER BY version DESC LIMIT 1`. Se não encontra, usa constante `_TOOL_INSTRUCTIONS`.

**Componentes registráveis**:
- `tool_instructions` — `_TOOL_INSTRUCTIONS`
- `index_instructions` — `_INDEX_INSTRUCTIONS`
- `injection_stagnation` — mensagem quando stagnation
- `injection_circuit_breaker` — mensagem quando circuit breaker
- `compaction_format` — template do resumo de compaction

#### H-07: Nível 1 — Parameter Tuning (B-65)

Job batch que lê scores agregados e ajusta parâmetros. Zero LLM.

```python
# harness/tuner.py
class ParameterTuner:
    def tune(self, symbiote_id: str) -> dict[str, Any]:
        scores = self._get_recent_scores(symbiote_id, days=7)
        traces = self._get_recent_traces(symbiote_id, days=7)
        adjustments = {}

        # Regra 1: max_iterations
        max_iter_sessions = [t for t in traces if t.stop_reason == "max_iterations"]
        if len(max_iter_sessions) / len(traces) > 0.30:
            adjustments["max_tool_iterations"] = current + 5  # cap at 30

        # Regra 2: compaction_threshold
        avg_iters = mean(t.total_iterations for t in traces if t.stop_reason == "end_turn")
        if avg_iters < current_compaction_threshold:
            adjustments["compaction_threshold"] = max(int(avg_iters) - 1, 2)

        # Regra 3: microcompact chars
        # (requer tracking se truncation correlaciona com score baixo)

        return adjustments
```

**Invocação**: `symbiote tune <symbiote_id>` na CLI, ou cron no host, ou hook no `close_session()` a cada N sessões.

**Observação**: precisa de mínimo de sessões (ex: 50) antes de ajustar. Sem dados suficientes → noop. Cada ajuste é logado no audit_log para rastreabilidade.

#### H-08: max_iterations configurável (B-32)

Pré-requisito para H-07 funcionar. Adicionar `max_tool_iterations: int = 10` no EnvironmentConfig.

```sql
ALTER TABLE env_configs ADD COLUMN max_tool_iterations INTEGER DEFAULT 10;
```

`ChatRunner.run()` lê do context ao invés de usar `_MAX_TOOL_ITERATIONS`:
```python
max_iters = context.max_tool_iterations if context.tool_loop else 1
```

**Observação**: `AssembledContext` precisa de campo novo `max_tool_iterations`. O `ContextAssembler` propaga do EnvironmentConfig.

---

### Fase 3 — Prompt Evolution (quando Fase 2 mostrar que parameter tuning funciona)

> Objetivo: o harness evolui os TEXTOS que controlam o LLM.
> Pré-requisito: Fase 2 calibrada + 500+ sessões com scores.

#### H-09: Nível 2 — HarnessEvolver (B-67)

Job batch que usa LLM barato para propor textos melhores.

**Fluxo**:
```
1. Coletar sessões com score < 0.5 (últimos 7 dias)
2. Coletar sessões com score > 0.8 (para contraste)
3. Extrair traces dessas sessões
4. Montar prompt para proposer:
   "Instruções atuais: {current}
    Sessões que falharam: {failed_summary}
    Sessões que deram certo: {success_summary}
    Proponha versão melhorada que endereçe os padrões de falha."
5. Proposer retorna texto novo
6. Guard rails: max length, manter linhas CRITICAL, etc.
7. Salvar como nova versão em harness_versions
8. Após 50+ sessões, comparar avg_score
9. Se pior: rollback. Se melhor: manter.
```

**Guard rails obrigatórios**:
- Versão nova não pode ter > 2x o tamanho da anterior
- Linhas contendo "CRITICAL" na versão atual devem existir na nova
- Se o proposer retorna lixo (JSON, código, etc.), descartar
- Mínimo 50 sessões antes de aceitar/rejeitar
- Rollback automático se `new_avg < old_avg - 0.05`

**Observação sobre custo**: 1 call de Haiku por batch (~semanal). Com traces de ~20 sessões no prompt, são ~10k tokens input. Custo negligível.

**Observação sobre o proposer**: idealmente o proposer é um modelo DIFERENTE do que roda no harness. Se o harness usa Kimi/Groq, o proposer pode ser Haiku. Se o harness usa Claude, o proposer pode ser GPT. Isso evita que o proposer tenha os mesmos blind spots do modelo que está tentando melhorar.

#### H-10: Memory/Knowledge on-demand (B-68)

Mudar de context pre-packed para acesso adaptativo.

**Implementação**:
- Registrar `search_memories(query, scope?, limit?)` e `search_knowledge(query, limit?)` como builtin tools
- Novo `EnvironmentConfig.context_mode: Literal["packed", "on_demand"]` (default: packed)
- Quando on-demand: ContextAssembler pula memories/knowledge, tools ficam disponíveis
- System prompt ganha: "Você tem acesso a memórias e knowledge via tools. Use quando precisar de contexto."

**Observação sobre trade-offs**:
- On-demand gasta mais iterations (LLM precisa chamar tool antes de responder)
- Mas contexto injetado é mais preciso (LLM formula a query certa)
- Ideal para symbiotas com MUITAS memórias onde pre-packed polui
- NÃO ideal para conversas rápidas (1 iteration extra = latência)
- Pode ser híbrido: packed para top-3 memories + on-demand para deep search

---

### Fase 4 — Horizonte Longo (futuro, quando tiver benchmark suite)

> Não implementar agora. Documentar para quando fizer sentido.

#### H-11: Benchmark Suite próprio

Criar um conjunto de tasks representativos dos hosts (YouNews, etc.) com grading automático. Tipo:
- "Publique a matéria sobre incêndio" → grading: items_publish foi chamado com status=ready? 1.0/0.0
- "Busque matérias de ontem" → grading: items_list foi chamado com date filter? 1.0/0.0

Isso permitiria rodar o Meta-Harness loop completo: propor harness → eval contra benchmark → score → iterar.

#### H-12: Nível 3 — Structural Evolution

Coding agent que reescreve partes do código do ChatRunner. Precisa de:
- Benchmark suite (H-11)
- Sandbox eval (rodar harness candidato sem afetar produção)
- Rollback de código (git branch por variante)

Complexidade desproporcional para hoje. Registrar como horizonte.

#### H-13: Cross-symbiote learning

Se o symbiote A descobre que "verificar status antes de publicar" melhora o score, transferir esse aprendizado para o symbiote B que usa tools similares. Requer:
- Tagging de tools cross-symbiote (B usa tools parecidas com A?)
- Propagação de harness_versions entre symbiotas com overlap de tools

---

## Observações de Implementação

### Fluxo de dados no close_session (onde tudo converge)

Hoje o `close_session()` faz:
```
1. reflection.reflect_session() → extrai fatos
2. sessions.close() → status=closed, summary
```

Com o harness plan, precisa fazer:
```
1. Persist LoopTrace → execution_traces (H-05)
2. Compute SessionScore → session_scores (H-01)
3. Generate MemoryEntry de falha se aplicável (H-03)
4. reflection.reflect_session() → extrai fatos (existente)
5. sessions.close() → status=closed, summary (existente)
6. Trigger parameter tuning se N sessões acumuladas (H-07, eventual)
```

**Problema**: `close_session()` hoje não tem acesso ao `LoopTrace`. O trace vive no `RunResult` que é retornado ao caller em `kernel.message()`. Precisamos persistir o trace ANTES do close.

**Solução proposta**: `kernel.message()` persiste o trace imediatamente (se houver) em `execution_traces` E guarda `self._last_trace = trace`. O `close_session()` usa `self._last_trace` para scoring e MemoryEntry de falha. Limpa `self._last_trace` após uso.

### SQLite schema migrations

Todas as migrations devem ser idempotentes (ALTER TABLE IF NOT EXISTS pattern do SQLiteAdapter). Ordem:

```sql
-- Fase 1
CREATE TABLE IF NOT EXISTS execution_traces (...);
CREATE TABLE IF NOT EXISTS session_scores (...);
ALTER TABLE env_configs ADD COLUMN memory_share REAL DEFAULT 0.40;
ALTER TABLE env_configs ADD COLUMN knowledge_share REAL DEFAULT 0.25;

-- Fase 2
CREATE TABLE IF NOT EXISTS harness_versions (...);
ALTER TABLE env_configs ADD COLUMN max_tool_iterations INTEGER DEFAULT 10;

-- Fase 3
ALTER TABLE env_configs ADD COLUMN context_mode TEXT DEFAULT 'packed';
```

### Backward compatibility

**Regra absoluta**: se o host não configura nada novo, o comportamento é idêntico ao atual. Defaults são os valores hardcoded de hoje. Nenhuma feature de evolução é ativa por padrão — o host opt-in.

### Métricas para observar antes de avançar fases

**Para sair da Fase 1 → Fase 2**:
- [ ] Pelo menos 200 sessões com `session_scores` persistidas
- [ ] Distribuição de stop_reasons estável (não está mudando semana a semana)
- [ ] Pelo menos 1 host reportando feedback via FeedbackPort (nice to have, não blocker)
- [ ] MemoryEntry de falha aparecendo em `get_relevant()` em sessões subsequentes (validar manualmente)

**Para sair da Fase 2 → Fase 3**:
- [ ] Parameter tuning rodou pelo menos 5 vezes
- [ ] Pelo menos 1 ajuste automático de parâmetro mostrou melhoria no avg_score
- [ ] Pelo menos 500 sessões com scores
- [ ] harness_versions table com pelo menos 1 versão customizada ativa (mesmo que manual)

**Para sair da Fase 3 → Fase 4**:
- [ ] HarnessEvolver gerou pelo menos 3 versões aceitas (não rolled back)
- [ ] avg_score do symbiote melhorou vs. baseline (default hardcoded)
- [ ] Benchmark suite definido com pelo menos 20 tasks com grading automático

---

## Resumo Executivo

| Fase | O que | Quando | Risco | Custo LLM |
|---|---|---|---|---|
| **1 — Fundações** | Score + traces + feedback + memory de falha | Agora | Nenhum | Zero |
| **2 — Calibração** | Parameter tuning automático | Com 200+ sessões | Baixo (caps + fallback) | Zero |
| **3 — Evolução** | Prompt evolution + on-demand context | Com 500+ sessões | Médio (rollback) | ~1 Haiku/semana |
| **4 — Horizonte** | Benchmark suite + structural evolution | Quando justificar | Alto | Variável |

O caminho é incremental. Cada fase se paga antes de abrir a próxima. Se a Fase 1 não mostrar valor (scores não correlacionam com qualidade percebida), paramos ali. Se a Fase 2 mostrar que parameter tuning melhora scores, avançamos. A Fase 4 pode nunca ser necessária — e tudo bem.
