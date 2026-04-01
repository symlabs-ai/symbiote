# Backlog — Symbiote

> Popule via `/backlog <descrição>` em maintenance mode.

## Ideias

| # | Descrição | Origem | Prioridade | Status |
|---|-----------|--------|------------|--------|
| B-27 | Tool Loop — streaming mid-loop | 2026-03-19, dev | média | |
| B-29 | Tool Loop — human-in-the-loop | 2026-03-19, dev | média | |
| B-30 | Tool Loop — working memory intermediária | 2026-03-19, dev | média | |
| B-32 | Tool Loop — max_iterations configurável | 2026-03-19, dev | baixa | |
| B-33 | Tool Loop — timeout | 2026-03-19, dev | alta | |
| B-34 | Tool Loop — index mode cache | 2026-03-19, dev | média | |
| B-35 | Tool Loop — teste multi-modelo | 2026-03-19, dev | média | |
| B-36 | Tool Loop — custo tracking | 2026-03-19, dev | alta | |
| B-40 | Tool Mode — refatorar tool_loop:bool para tool_mode:Literal["instant","brief","continuous"] | 2026-03-19, dev | alta | |
| B-41 | Kimi K2 context limit — plano pago resolveu o 500, mas context_length_exceeded persiste com 75+ tools no index; queries multi-step estouram. Bloqueador para tool loops complexos. Precisa de semantic loading ou redução de tags | 2026-03-20, harness | alta | monitoramento |
| B-42 | Tool descriptions genéricas — melhorar summaries no OpenAPI do YouNews (ex: "Item Action" → "Change item status") | 2026-03-20, harness | média | cross-repo:younews |
| B-44 | Validar supressão de narração intermediária no YouNews staging após deploy v0.17.31+ | 2026-03-20, harness | média | cross-repo:younews |
| B-45 | Test harness pytest — validar tests/e2e/test_kimi_tool_loop.py (requer YouNews + SymGateway rodando) | 2026-03-20, harness | baixa | |
| B-64 | harness_versions — versionamento de textos evolvable por symbiote | 2026-04-01, meta-harness | média | |
| B-65 | Nível 1 Parameter Tuning — auto-calibração de parâmetros baseada em scores | 2026-04-01, meta-harness | média | |
| B-67 | Nível 2 HarnessEvolver — proposer LLM evolui _TOOL_INSTRUCTIONS offline | 2026-04-01, meta-harness | alta | |
| B-68 | Memory/Knowledge on-demand — search_memories como tool em vez de pre-packed | 2026-04-01, meta-harness | média | |

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
