# SPEC.md — Symbiote

> Versão: 0.2.27
> MVP entregue em: 2026-03-16
> Última atualização: 2026-04-01
> Status: maintenance

---

## Visão

Symbiote é um kernel Python para construir entidades cognitivas persistentes. Modela simbiótas — instâncias com identidade, memória em camadas, workspace, environment, processo e reflexão — em vez de tarefas efêmeras.

**Problema**: Frameworks de agentes modelam "tarefa", não "entidade". Resultado: agentes amnésicos, contexto despejado sem curadoria, zero continuidade entre sessões.

**Público-alvo**: Desenvolvedores Python que constroem assistentes e agentes persistentes; equipes que embarcam IA em produtos internos.

**Diferencial**: Entidade como unidade de abstração, memória em 4 camadas com curadoria, workspace/environment first-class, reflexão obrigatória, local-first (SQLite + filesystem).

---

## Escopo

### O que está incluso

| Feature | User Story | Status | Ciclo |
|---------|-----------|--------|-------|
| Identidade persistente com persona e audit trail | US-01 | ✅ Entregue | cycle-01 |
| Sessões com ciclo de vida completo (start, resume, close, summary) | US-02 | ✅ Entregue | cycle-01 |
| Workspace com workdir ativo e artefatos no filesystem | US-03 | ✅ Entregue | cycle-01 |
| Environment com tools, policies e deny-by-default | US-04 | ✅ Entregue | cycle-01 |
| Knowledge layer separado de memória | US-05 | ✅ Entregue | cycle-01 |
| Memória em 4 camadas (working, session, long-term, semantic recall interface) | US-06 | ✅ Entregue | cycle-01 |
| Context assembly seletivo com orçamento configurável | US-07 | ✅ Entregue | cycle-01 |
| Runners especializados (chat, task, process) com registry | US-08 | ✅ Entregue | cycle-01 |
| Tools com policy gate e audit log | US-09 | ✅ Entregue | cycle-01 |
| Process engine declarativo com 5 definições default | US-10 | ✅ Entregue | cycle-01 |
| 6 capacidades: Learn, Teach, Chat, Work, Show, Reflect | US-11 | ✅ Entregue | cycle-01 |
| Reflexão com extração de fatos duráveis | US-12 | ✅ Entregue | cycle-01 |
| Export Markdown auditável (sessões, memórias, decisões) | US-13 | ✅ Entregue | cycle-01 |
| Biblioteca Python + CLI + API HTTP | US-14 | ✅ Entregue | cycle-01 |

### Pós-MVP (Sprint Backlog — 2026-03-17)

| Feature | Backlog | Status | Sprint |
|---------|---------|--------|--------|
| Tool Error Hints — retry hints automáticos em tool calls com erro | B-8 | ✅ Entregue | backlog-sprint |
| Runtime Context Strip — metadata efêmera no prompt sem poluir histórico | B-9 | ✅ Entregue | backlog-sprint |
| MessageRepository port — isolamento de SQL do ReflectionEngine | B-3 | ✅ Entregue | backlog-sprint |
| Memory Consolidation — sumarização via LLM quando tokens excedem threshold | B-10 | ✅ Entregue | backlog-sprint |
| Subagent Spawning — delegação entre Symbiotas com recursion guard | B-11 | ✅ Entregue | backlog-sprint |
| MessageBus — fila async inbound/outbound para channels | B-12 | ✅ Entregue | backlog-sprint |
| Progressive Skills — skills .md com carregamento lazy | B-13 | ✅ Entregue | backlog-sprint |
| Semantic Recall — busca por keywords com scoring | B-4 | ✅ Entregue | backlog-sprint |
| ProcessEngine Cache Invalidation — TTL-based com invalidate_cache() | B-6 | ✅ Entregue | backlog-sprint |
| Interactive CLI Chat — REPL loop com /quit, /reflect | B-2 | ✅ Entregue | backlog-sprint |
| LLM E2E Integration Tests — 5 testes skipáveis para LLM real | B-5 | ✅ Entregue | backlog-sprint |
| Docker Container — multi-stage Dockerfile com health check | B-1 | ✅ Entregue | backlog-sprint |

### Pós-MVP (Sprint Security — 2026-03-17)

| Feature | Backlog | Status | Sprint |
|---------|---------|--------|--------|
| SSRF Protection — validação de IP em HTTP tools + redirect guard | B-14 | ✅ Entregue | security-sprint |
| Untrusted Content Banner — banner anti-prompt-injection em respostas HTTP | B-15 | ✅ Entregue | security-sprint |
| Tool Call Pair Consistency — trim de WorkingMemory alinhado a turn boundaries | B-18 | ✅ Entregue | security-sprint |
| GenerationSettings — temperature/max_tokens/reasoning_effort pass-through | B-17 | ✅ Entregue | security-sprint |
| Async Memory Consolidation — trim imediato + LLM summarization em background | B-16 | ✅ Entregue | security-sprint |

### Pós-MVP (Sprint Hosted Service — 2026-03-17)

| Feature | Backlog | Status | Sprint |
|---------|---------|--------|--------|
| API Key Auth — Bearer token com SHA-256, tenant isolation, admin/user roles | B-19 | ✅ Entregue | hosted-sprint |
| Chat Endpoint — POST /sessions/{id}/chat com kernel.message() via HTTP | B-20 | ✅ Entregue | hosted-sprint |
| Multi-tenant Isolation — owner_id enforcement, tenant scoping em endpoints | B-21 | ✅ Entregue | hosted-sprint |
| Python SDK — SymbioteClient thin HTTP client com httpx | B-22 | ✅ Entregue | hosted-sprint |

### Pós-MVP (Sprint Nanobot Adaptations — 2026-03-30)

| Feature | Backlog | Status | Sprint |
|---------|---------|--------|--------|
| Prompt Cache Integration — forge_llm prompt_caching via EnvironmentConfig | B-46 | ✅ Entregue | nanobot-sprint |
| Message Retry + Backoff — exponential backoff no MessageBus | B-47 | ✅ Entregue | nanobot-sprint |
| Per-Session Locks — SessionLock sync/async no kernel | B-48 | ✅ Entregue | nanobot-sprint |
| Hardened allow_internal — exclude from serialization + audit log | B-49 | ✅ Entregue | nanobot-sprint |
| CompositeHook — lifecycle hooks composáveis com error isolation | B-50 | ✅ Entregue | nanobot-sprint |
| Delta Streaming — StreamDelta no MessageBus para canais real-time | B-51 | ✅ Entregue | nanobot-sprint |

### Pós-MVP (Sprint Hermes Adaptations — 2026-03-30)

| Feature | Backlog | Status | Sprint |
|---------|---------|--------|--------|
| SessionRecallPort — protocol para busca host-provided em sessões | B-52 | ✅ Entregue | hermes-sprint |
| MemoryCategory — auto-classificação ephemeral/declarative/procedural/meta | B-53 | ✅ Entregue | hermes-sprint |
| Context Compaction — compactação mid-loop do tool loop | B-54 | ✅ Entregue | hermes-sprint |

### Pós-MVP (Sprint Agent Loop Resilience — 2026-03-31)

| Feature | Backlog | Status | Sprint |
|---------|---------|--------|--------|
| Parallel tool execution — asyncio.gather + ThreadPoolExecutor | B-55 | ✅ Entregue | resilience-sprint |
| LLM retry with exponential backoff — 3 retries, 1s/2s/4s | B-56 | ✅ Entregue | resilience-sprint |
| Diminishing returns detection + circuit breaker — LoopController | B-57 | ✅ Entregue | resilience-sprint |
| 3-layer compaction — microcompact + loop compact + autocompact | B-58 | ✅ Entregue | resilience-sprint |

### Pós-MVP (Sprint Harness Foundations — 2026-04-01)

| Feature | Backlog | Status | Sprint |
|---------|---------|--------|--------|
| SessionScore — auto_score from LoopTrace + user feedback composition | B-60 | ✅ Entregue | harness-foundations |
| FeedbackPort — protocol para host reportar qualidade de sessão | B-61 | ✅ Entregue | harness-foundations |
| MemoryEntry de falha — fato procedural determinístico quando loop falha | B-62 | ✅ Entregue | harness-foundations |
| Context splits configuráveis — memory_share/knowledge_share per symbiote | B-63 | ✅ Entregue | harness-foundations |
| LoopTrace persistence — execution_traces table no SQLite | B-66 | ✅ Entregue | harness-foundations |

### Pós-MVP (Sprint Harness Evolution — 2026-04-01)

| Feature | Backlog | Status | Sprint |
|---------|---------|--------|--------|
| max_tool_iterations configurável — per symbiote via EnvironmentConfig | B-32/B-65 | ✅ Entregue | harness-evolution |
| harness_versions — versionamento de textos evolvable por symbiote | B-64 | ✅ Entregue | harness-evolution |
| ParameterTuner — auto-calibração tiered (Tier 0-3) com safety caps | B-65 | ✅ Entregue | harness-evolution |

### Pós-MVP (Sprint Prompt Evolution — 2026-04-01)

| Feature | Backlog | Status | Sprint |
|---------|---------|--------|--------|
| HarnessEvolver — LLM proposer evolui tool_instructions offline com guard rails | B-67 | ✅ Entregue | prompt-evolution |
| Evolvable text bridge — overrides flow ContextAssembler → ChatRunner → LoopController | B-67 | ✅ Entregue | prompt-evolution |
| kernel.set_evolver_llm() — host injeta proposer LLM separado (opção 3) | B-67 | ✅ Entregue | prompt-evolution |

### Pós-MVP (Sprint Horizon — 2026-04-01)

| Feature | Backlog | Status | Sprint |
|---------|---------|--------|--------|
| Timeout — per-tool (30s) + loop total (300s) configuráveis | B-33 | ✅ Entregue | horizon |
| Human-in-the-loop — risk_level + approval callback | B-29 | ✅ Entregue | horizon |
| Index mode cache — loop-local schema cache | B-34 | ✅ Entregue | horizon |
| Multi-model test matrix — E2E test infra | B-35 | ✅ Entregue | horizon |
| Tool Mode — instant/brief/long_run/continuous (4 modes) | B-40 | ✅ Entregue | horizon |
| Instant Mode — fast-path, mode-aware scoring, context seletivo | — | ✅ Entregue | v0.3.1 |
| Brief Mode — sync trace, calibrated scoring, multi-step instructions | — | ✅ Entregue | v0.3.2 |
| Long-run Mode — Planner/Generator/Evaluator architecture | — | ✅ Entregue | v0.3.3 |
| Streaming mid-loop — on_progress + on_stream callbacks | B-27 | ✅ Entregue | horizon |
| Working memory intermediária — loop summary in WorkingMemory | B-30 | ✅ Entregue | horizon |
| Memory/Knowledge on-demand — search tools | B-68 | ✅ Entregue | horizon |
| Benchmark Suite — BenchmarkRunner with task grading | H-11 | ✅ Entregue | horizon |
| Structural Evolution — StructuralEvolver with strategy registry | H-12 | ✅ Entregue | horizon |
| Cross-Symbiote Learning — tool overlap + version transfer | H-13 | ✅ Entregue | horizon |

### O que está fora do escopo

- Multi-tenant completo
- ACL corporativa avançada
- Marketplace de simbiótas
- Interface visual rica (UI web)
- Voz e multimodalidade avançada
- Treinamento/fine-tuning do modelo
- Banco vetorial obrigatório
- Colaboração multiusuário concorrente
- Engine de agendamento complexo

---

## Funcionalidades Principais

### Identidade e Persona
Simbióta com id, name, role, persona configurável e audit trail de alterações.
**User Story**: US-01
**Entrypoint**: `symbiote create --name X --role Y --persona-json '{}'`

### Sessões
Ciclo completo: start → messages → decisions → close (com summary e reflexão).
**User Story**: US-02
**Entrypoint**: `symbiote session start <SYMBIOTE_ID> --goal "..."` / `POST /sessions`

### Chat (Value Track)
Conversa contextual via LLM com memória seletiva e orçamento de contexto.
**User Story**: US-11
**Entrypoint**: `symbiote chat <SESSION_ID> "mensagem"` / `POST /sessions/{id}/messages`

### Learn (Value Track)
Persistir fatos duráveis como memória de longo prazo (preferências, procedimentos, restrições).
**User Story**: US-11
**Entrypoint**: `symbiote learn <SESSION_ID> "fato" --type preference --importance 0.9`

### Teach (Value Track)
Explicação estruturada usando knowledge + memórias relevantes.
**User Story**: US-11
**Entrypoint**: `symbiote teach <SESSION_ID> "tema"`

### Work (Value Track)
Execução de tarefas via runners especializados com seleção por intent.
**User Story**: US-11
**Entrypoint**: `symbiote work <SESSION_ID> "task description" --intent chat`

### Show (Value Track)
Exibição de dados relevantes (sessão, memórias, knowledge) em Markdown formatado.
**User Story**: US-11
**Entrypoint**: `symbiote show <SESSION_ID> "query"`

### Reflect (Value Track)
Extração de fatos duráveis, descarte de ruído, geração de summary.
**User Story**: US-12
**Entrypoint**: `symbiote reflect <SESSION_ID>`

### Context Assembly
Pipeline: identity → working memory → memories → knowledge → rank → trim → assemble. Orçamento configurável.
**User Story**: US-07
**Entrypoint**: via biblioteca (`ContextAssembler.build()`)

### Policy Gate
Deny-by-default. Tools autorizadas via EnvironmentManager. Audit log com timestamp.
**User Story**: US-04, US-09
**Entrypoint**: via biblioteca (`PolicyGate.check()`)

### Export
Sessões, memórias e decisões exportáveis em Markdown legível.
**User Story**: US-13
**Entrypoint**: `symbiote export session <SESSION_ID>`

---

## Tech Stack

| Camada | Tecnologia | Motivo |
|--------|-----------|--------|
| Linguagem | Python 3.12+ | Ecossistema AI, tipagem, async |
| Framework base | ForgeBase (Clean/Hex) | Arquitetura, UseCaseRunner, Pulse |
| Modelos | Pydantic v2 | Validação, serialização, config |
| Persistência | SQLite (stdlib) | Local-first, zero infra, 15+ tabelas |
| CLI | Typer + Rich | Type hints nativos, formatação Rich |
| HTTP | FastAPI + Uvicorn | Async, Pydantic integration, OpenAPI |
| LLM | LLMPort + ForgeLLM/Mock | Abstração fina, sem acoplamento |
| MCP | McpToolProvider + forge_llm | Model Context Protocol tool bridging |
| Harness Evolution | ParameterTuner + HarnessEvolver | Auto-calibração tiered + evolução de prompt via LLM |
| Observabilidade | structlog | Logs JSON estruturados |
| Testes | pytest (900+ testes) | TDD, cobertura ~85% |

> Detalhes completos: `project/docs/tech_stack.md`

---

## Arquitetura

```
CLI / HTTP API / Python Library / SDK / MessageBus
        |
    SymbioteKernel (orchestrator)
        |
    CapabilitySurface (learn, teach, chat, work, show, reflect)
        |
    +-----------------------------------------------+
    |  ContextAssembler    RunnerRegistry            |
    |  ReflectionEngine    ProcessEngine             |  Cognitive Layer
    |  MemoryConsolidator  SubagentManager           |
    |  SkillsLoader        RuntimeContext            |
    +---+-------------------------------------------+
    +---+-------------------------------------------+
    |  IdentityManager     SessionManager            |
    |  MemoryStore         KnowledgeService          |  State Layer
    |  WorkspaceManager    EnvironmentManager         |
    |  SemanticRecallProvider  MessageRepository      |
    +---+-------------------------------------------+
    +---+-------------------------------------------+
    |  ToolGateway         PolicyGate                |
    |  LoopController      ChatRunner                |  Execution Layer
    |  SessionLock         CompositeHook             |
    +---+-------------------------------------------+
    +---+-------------------------------------------+
    |  ParameterTuner      HarnessEvolver            |
    |  HarnessVersionRepo  BenchmarkRunner           |  Harness Layer
    |  StructuralEvolver   CrossSymbioteLearner      |
    +---+-------------------------------------------+
    +---+-------------------------------------------+
    |  SQLiteAdapter       ForgeLLMAdapter           |  Adapter Layer
    |  ExportService       McpToolProvider           |
    +---+-------------------------------------------+
    +---+-------------------------------------------+
    |  SQLite DB  .  Filesystem  .  Docker           |  Persistence
    +-----------------------------------------------+
```

> Diagramas: `project/docs/diagrams/` (class, components, database, architecture)

---

## Modo de Manutenção

Este projeto está em **maintenance mode**. Novas features são adicionadas via:

```
/feature <descrição da feature>
```

A skill `/feature` lê este SPEC.md para entender o contexto antes de implementar.
Ao finalizar uma feature (`/feature done`), SPEC.md é atualizado automaticamente.

### Convenções do projeto

- Arquitetura Clean/Hex via ForgeBase: domínio puro em `core/`, adapters em `adapters/`
- Ports como Protocols em `core/ports.py` — nunca importar implementação concreta no core
- Domain exceptions em `core/exceptions.py` — nunca usar `ValueError` ou `RuntimeError`
- Testes em `tests/unit/` (com SQLiteAdapter real) e `tests/e2e/` (cenários ponta-a-ponta)
- Smoke tests em `tests/smoke/` (shell scripts exercendo CLI real)
- E2E LLM tests em `tests/e2e/` (skipáveis, `SYMBIOTE_E2E_LLM=1`)
- datetime: sempre `fromisoformat()` explícito ao ler do DB
- Tags/JSON: `json.dumps()` na escrita, `json.loads()` na leitura, coluna `*_json`
- Commits no formato `feat(sprint-XX): descrição` ou `feat(T-XX): descrição`
- Pre-commit hooks: ruff + trailing whitespace + end-of-file + check-yaml
- Ports como Protocols: `MessagePort`, `MemoryPort`, `LLMPort`, `KnowledgePort`

---

> Histórico de mudanças: `CHANGELOG.md`
