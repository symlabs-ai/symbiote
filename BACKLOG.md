# Backlog — Symbiote

> Popule via `/backlog <descrição>` em maintenance mode.

## Ideias

| # | Descrição | Origem | Prioridade | Status |
|---|-----------|--------|------------|--------|
| B-36 | Tool Loop — custo tracking | 2026-03-19, dev | alta | |
| B-41 | Kimi K2 context limit — plano pago resolveu o 500, mas context_length_exceeded persiste com 75+ tools no index; queries multi-step estouram. Bloqueador para tool loops complexos. Precisa de semantic loading ou redução de tags | 2026-03-20, harness | alta | monitoramento |
| B-42 | Tool descriptions genéricas — melhorar summaries no OpenAPI do YouNews (ex: "Item Action" → "Change item status") | 2026-03-20, harness | média | cross-repo:younews |
| B-44 | Validar supressão de narração intermediária no YouNews staging após deploy v0.17.31+ | 2026-03-20, harness | média | cross-repo:younews |
| B-45 | Test harness pytest — validar tests/e2e/test_kimi_tool_loop.py (requer YouNews + SymGateway rodando) | 2026-03-20, harness | baixa | |
| B-46 | **[BUG] 4 testes pré-existentes falhos em test_generation_settings + test_instant_mode**: assertions mockam `_run_instant(ctx, on_token)` sem o kwarg `llm_config=None` adicionado na v0.5.0. Fix trivial: atualizar 4 assert chamadas pra `_run_instant(ctx, on_token, llm_config=None)`. Não é regressão funcional, é só drift de teste. Ver F3 detalhamento abaixo. | 2026-05-27, sprint5-followup | baixa | bug |
| B-47 | **[BUG] `SQLiteAdapter` single-cursor thread safety**: conexão única compartilhada entre threads. Race em writes/reads concorrentes manifesta como `sqlite3.InterfaceError: bad parameter or other API misuse` (visto no teste H1 do Sprint 4.1). Mitigado em produção single-user (rotinas mid-session são sequenciais), mas qualquer nova feature paralela morre nele. Fix: lock em `SQLiteAdapter` ou connection pool. Ver F4 detalhamento. | 2026-05-27, sprint5-followup | média | bug |

### Detalhamento dos itens pendentes

**B-27 — Streaming mid-loop.** O `on_token` callback só emite tokens quando não há tool calls. Nas iterações intermediárias do loop, o usuário vê silêncio total até o loop terminar. Para UX aceitável, o host precisa receber tokens em todas as iterações, com indicação de progresso entre os passos.

**B-29 — Human-in-the-loop.** O host poderia classificar tools por nível de risco — tools de leitura rodam sem aprovação, tools de escrita pausam o loop e pedem confirmação. Implementação: callback `on_before_tool_call(tool_id, params) -> bool` e/ou `risk_level` no ToolDescriptor.

**B-30 — Working memory intermediária.** O ChatRunner salva apenas a resposta final na WorkingMemory. Passos intermediários são perdidos. Opções: salvar todos os passos (verbose) ou resumo estruturado (compacto).

**B-32 — max_iterations configurável.** Adicionar `max_tool_iterations: int = 10` no EnvironmentConfig por symbiote, com teto global como safety net.

**B-33 — Timeout.** Dois mecanismos: (1) timeout por tool call no ToolGateway.execute; (2) timeout total do loop no ChatRunner.

**B-34 — Index mode cache.** Cache de schemas já fetchados na sessão do loop para evitar calls repetidas a `get_tool_schema`.

**B-35 — Teste multi-modelo.** Matriz de testes {modelo × modo × query} com métricas de iterações, taxa de sucesso e custo.

**B-36 — Custo tracking.** Medir tokens in/out por iteração, custo acumulado por request, budget máximo por request.

**B-40 — Tool Mode: instant, brief, continuous.** Refatorar `tool_loop: bool` para `tool_mode: Literal["instant", "brief", "continuous"]`. Detalhes no `/kb/ralph-loop-analysis.md`.

### Detalhamento dos itens Meta-Harness (B-60 a B-68)

Documentação completa em `~/dev/kb/engenharia/meta_harness.md`, seção 4 (Backlog de Implementação).

## Implementadas

| # | Descrição | Implementada em | Versão |
|---|-----------|-----------------|--------|
| B-33 | Timeout — per-tool (30s) + loop total (300s) configuráveis per symbiote | 2026-04-01 | 0.2.27 |
| B-29 | Human-in-the-loop — risk_level + approval callback | 2026-04-01 | 0.2.27 |
| B-34 | Index mode cache — loop-local schema cache para index mode | 2026-04-01 | 0.2.27 |
| B-35 | Multi-model test matrix — E2E infra com 3 cenários × 3 modelos | 2026-04-01 | 0.2.27 |
| B-40 | Tool Mode — instant/brief/continuous replaces binary tool_loop | 2026-04-01 | 0.2.27 |
| B-27 | Streaming mid-loop — on_progress + on_stream callbacks | 2026-04-01 | 0.2.27 |
| B-30 | Working memory intermediária — loop summary in WorkingMemory | 2026-04-01 | 0.2.27 |
| B-68 | Memory/Knowledge on-demand — search_memories/search_knowledge tools | 2026-04-01 | 0.2.27 |
| H-11 | BenchmarkRunner — task grading (tool_called, param_match, custom) | 2026-04-01 | 0.2.27 |
| H-12 | StructuralEvolver — pluggable strategy registry | 2026-04-01 | 0.2.27 |
| H-13 | CrossSymbioteLearner — tool overlap + version transfer | 2026-04-01 | 0.2.27 |
| B-67 | HarnessEvolver — LLM proposer evolui tool_instructions com guard rails + auto-rollback | 2026-04-01 | 0.2.26 |
| B-32 | max_tool_iterations configurável — per symbiote via EnvironmentConfig, cap 50 | 2026-04-01 | 0.2.25 |
| B-64 | harness_versions — versionamento de textos evolvable por symbiote com rollback | 2026-04-01 | 0.2.25 |
| B-65 | ParameterTuner — auto-calibração tiered (Tier 0-3) com safety caps e logging | 2026-04-01 | 0.2.25 |
| B-60 | SessionScore — auto_score from LoopTrace + user feedback composition | 2026-04-01 | 0.2.24 |
| B-61 | FeedbackPort — protocol para host reportar qualidade de sessão | 2026-04-01 | 0.2.24 |
| B-62 | MemoryEntry de falha — fato procedural determinístico quando loop falha | 2026-04-01 | 0.2.24 |
| B-63 | Context splits configuráveis — memory_share/knowledge_share per symbiote | 2026-04-01 | 0.2.24 |
| B-66 | LoopTrace persistence — execution_traces table no SQLite | 2026-04-01 | 0.2.24 |
| B-55 | Parallel tool execution — asyncio.gather (async) + ThreadPoolExecutor (sync) com max_workers=4 | 2026-03-31 | 0.2.22 |
| B-56 | LLM retry with exponential backoff — 3 retries, 1s/2s/4s, only transient errors | 2026-03-31 | 0.2.22 |
| B-57 | Diminishing returns detection + circuit breaker — LoopController com duplicate/failure detection. Cobre B-25 (LLM não sabe parar), B-28 (observability/LoopTrace), B-31 (circuit breaker) | 2026-03-31 | 0.2.22 |
| B-58 | 3-layer compaction — microcompact + loop compact + autocompact. Cobre B-26 (context growth), B-39 (Ralph-inspired compaction) | 2026-03-31 | 0.2.22 |
| B-46 | Prompt Cache Integration — forge_llm prompt_caching=True via EnvironmentConfig | 2026-03-30 | 0.2.20 |
| B-47 | Message Retry + Backoff — exponential backoff no MessageBus handler e respond() | 2026-03-30 | 0.2.20 |
| B-48 | Per-Session Locks — SessionLock sync/async no kernel.message() | 2026-03-30 | 0.2.20 |
| B-49 | Hardened allow_internal — exclude from serialization + audit log | 2026-03-30 | 0.2.20 |
| B-50 | CompositeHook — lifecycle hooks composáveis com error isolation | 2026-03-30 | 0.2.20 |
| B-51 | Delta Streaming — StreamDelta no MessageBus para canais real-time | 2026-03-30 | 0.2.20 |
| B-52 | SessionRecallPort — protocol para busca host-provided em sessões passadas | 2026-03-30 | 0.2.21 |
| B-53 | MemoryCategory — auto-classificação de memórias (ephemeral, declarative, procedural, meta) | 2026-03-30 | 0.2.21 |
| B-54 | Context Compaction — compactação mid-loop de mensagens do tool loop (Ralph-inspired) | 2026-03-30 | 0.2.21 |
| 23 | Deploy Hosted — porta 8008, symbiote.service, nginx + SSL em symbiote.symlabs.ai, CI/CD Gitea Actions | 2026-03-18 | 0.2.4 |
| 7 | MCP Integration — McpToolProvider bridges forge_llm ToolRegistry → ToolGateway; kernel.load_mcp_tools(registry, symbiote_id) | 2026-03-18 | 0.2.4 |
| 24 | DiscoveredToolLoader — bridge entre discovered_tools aprovadas e ToolGateway: kernel.load_discovered_tools() carrega tools com status=approved, registra como HttpTool e autoriza via EnvironmentManager | 2026-03-18 | 0.2.4 |
| 19 | API Key Auth — Bearer token, SHA-256, tenant isolation, admin/user roles | 2026-03-17 | 0.1.8 |
| 20 | Chat Endpoint — POST /sessions/{id}/chat com kernel.message() | 2026-03-17 | 0.1.8 |
| 21 | Multi-tenant Isolation — owner_id enforcement em endpoints | 2026-03-17 | 0.1.8 |
| 22 | Python SDK — SymbioteClient thin HTTP client com httpx | 2026-03-17 | 0.1.8 |
| 14 | SSRF Protection — validação de IP + redirect guard em HTTP tools | 2026-03-17 | 0.1.7 |
| 15 | Untrusted Content Banner — anti-prompt-injection em respostas HTTP | 2026-03-17 | 0.1.7 |
| 18 | Tool Call Pair Consistency — trim alinhado a turn boundaries | 2026-03-17 | 0.1.7 |
| 17 | GenerationSettings — pass-through de temperature/max_tokens/reasoning_effort | 2026-03-17 | 0.1.7 |
| 16 | Async Memory Consolidation — trim imediato + LLM em background thread | 2026-03-17 | 0.1.7 |
| 8 | Tool Error Hints — hint de retry automático em tool results com erro | 2026-03-17 | 0.1.5 |
| 9 | Runtime Context Strip — metadata efêmera no prompt sem poluir histórico | 2026-03-17 | 0.1.5 |
| 3 | MessageRepository port para isolar SQL do ReflectionEngine | 2026-03-17 | 0.1.5 |
| 10 | Memory Consolidation — sumarização automática via LLM quando tokens excedem threshold | 2026-03-17 | 0.1.5 |
| 11 | Subagent Spawning — delegação de tarefas entre Symbiotas com tool set restrito | 2026-03-17 | 0.1.5 |
| 12 | MessageBus — fila async inbound/outbound para desacoplar channels do kernel | 2026-03-17 | 0.1.5 |
| 13 | Progressive Skills — skills como .md no workspace, carregamento lazy por demanda | 2026-03-17 | 0.1.5 |
| 4 | Semantic recall provider (keyword-based MVP implementation) | 2026-03-17 | 0.1.5 |
| 6 | ProcessEngine cache invalidation para multi-worker | 2026-03-17 | 0.1.5 |
| 2 | Interactive chat mode na CLI (loop input/output) | 2026-03-17 | 0.1.5 |
| 5 | Integração com LLM real testada ponta-a-ponta | 2026-03-17 | 0.1.5 |
| 1 | Docker container de referência para modo serviço | 2026-03-17 | 0.1.5 |

---

## Detalhamento de bugs

### B-46 — Drift de testes em test_instant_mode + test_generation_settings

**Sintoma**: 4 testes falham consistentemente no `pytest tests/unit/`:

- `tests/unit/test_generation_settings.py::TestChatRunnerPassesConfig::test_none_config_passes_none`
- `tests/unit/test_instant_mode.py::TestInstantFastPath::test_instant_delegates_to_run_instant`
- `tests/unit/test_instant_mode.py::TestInstantFastPath::test_brief_uses_run_loop`
- `tests/unit/test_instant_mode.py::TestInstantFastPath::test_instant_async_delegates`

**Root cause confirmado**: assertions mockam o call de `_run_instant(ctx, on_token)` sem incluir o kwarg `llm_config=None` que foi adicionado em `v0.5.0` (feature "Per-call llm_config propaga kernel.message → ChatRunner.run → adapter config"). O `ChatRunner._run_instant` agora **sempre** recebe `llm_config=<value or None>` mas os testes não atualizaram.

**Diagnóstico** (exemplo de uma das falhas):

```
Expected: _run_instant(<AssembledContext...>, None)
Actual:   _run_instant(<AssembledContext...>, None, llm_config=None)
```

**Fix sugerido**: atualizar os 4 testes para incluir `llm_config=None` em `assert_called_with(...)`:

```diff
- mock_run_instant.assert_called_with(ctx, None)
+ mock_run_instant.assert_called_with(ctx, None, llm_config=None)
```

**Impacto**: zero em produção (só testes). Pré-existente desde v0.5.0. Sobreviveu a 5 sprints sem ser notado porque não é regressão de comportamento.

**Estimativa**: 4 edits + 1 rerun. <15min.

---

### B-47 — SQLiteAdapter single-cursor thread safety

**Sintoma**: `sqlite3.InterfaceError: bad parameter or other API misuse` quando ≥2 threads chamam `_storage.fetch_one` / `_storage.execute` simultaneamente.

**Manifestação histórica**:

- Apareceu no teste `TestKernelLazyBuildLock::test_concurrent_calls_build_engine_once` (Sprint 4.1, commit `a0c8c40`). O teste foi reescrito pra contornar (mock no `_environment.get_config`) — não corrigiu o adapter.
- Teste `TestSpawnDeduplication::test_concurrent_spawn_for_same_session_returns_single_thread` (mesma origem) precisou de `check_same_thread=False` na fixture pra rodar — mas isso desabilita a guarda, não corrige a race no cursor compartilhado.

**Root cause**: `src/symbiote/adapters/storage/sqlite.py` mantém um único `sqlite3.Connection` em `self._conn`. Métodos `execute` e `fetch_*` fazem `self._conn.execute(sql, params)` direto, sem lock. CPython sqlite3 não é thread-safe pra cursor compartilhado, mesmo com `check_same_thread=False`.

**Impacto atual em produção**:

- Single-user (caso comum): rotinas de `kernel.close_session` rodam sequencial → não atinge a race.
- Background threads que **escrevem** no DB: `MemoryConsolidator`, `BackgroundReviewEngine._write_audit`, `DreamEngine` async. Cada uma roda sozinha (1 thread por callsite), mas se 2 disparam simultâneo (ex: `close_session` chama spawn_final e `_maybe_dream` quase ao mesmo tempo), pode dar conflict.

**Fix sugerido**: 2 opções, qualquer uma viável.

1. **Lock no adapter** (mais simples):

   ```python
   class SQLiteAdapter:
       def __init__(self, ...):
           ...
           self._lock = threading.Lock()

       def execute(self, sql, params=None):
           with self._lock:
               cur = self._conn.execute(sql, params or ())
               self._conn.commit()
               return cur

       def fetch_one(self, sql, params=None):
           with self._lock:
               cur = self._conn.execute(sql, params or ())
               return dict(cur.fetchone()) if cur.fetchone() else None
       # ... idem fetch_all
   ```

   Custo: serializa todas as operações de SQLite (single-user OK, multi-tenant degrada).

2. **Connection pool** (mais escalável):

   ```python
   from queue import Queue
   class SQLiteAdapter:
       def __init__(self, ..., pool_size=4):
           self._pool = Queue()
           for _ in range(pool_size):
               self._pool.put(sqlite3.connect(...))

       def execute(self, sql, params=None):
           conn = self._pool.get()
           try:
               cur = conn.execute(sql, params or ())
               conn.commit()
               return cur
           finally:
               self._pool.put(conn)
   ```

   Custo: mais código, mas escala.

Decisão pendente do mantenedor. Pro caso single-user local-first, opção 1 é suficiente.

**Estimativa**: opção 1 = ~30 LoC + teste de stress concorrente. <2h.
