# SPEC.md — Symbiote

> Versão: 0.2.0
> MVP entregue em: 2026-03-16
> Última atualização: 2026-03-17
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
| Persistência | SQLite (stdlib) | Local-first, zero infra, 12 tabelas |
| CLI | Typer + Rich | Type hints nativos, formatação Rich |
| HTTP | FastAPI + Uvicorn | Async, Pydantic integration, OpenAPI |
| LLM | LLMPort + ForgeLLM/Mock | Abstração fina, sem acoplamento |
| Observabilidade | structlog | Logs JSON estruturados |
| Testes | pytest (393 testes) | TDD, cobertura ~96% |

> Detalhes completos: `project/docs/tech_stack.md`

---

## Arquitetura

```
CLI / HTTP API / Python Library / MessageBus
        │
    SymbioteKernel (orchestrator)
        │
    CapabilitySurface (learn, teach, chat, work, show, reflect)
        │
    ┌───┴───────────────────────────────────────┐
    │  ContextAssembler    RunnerRegistry        │  Cognitive Layer
    │  ReflectionEngine    ProcessEngine         │
    │  MemoryConsolidator  SubagentManager       │
    │  SkillsLoader        RuntimeContext        │
    └───┬───────────────────────────────────────┘
    ┌───┴───────────────────────────────────────┐
    │  IdentityManager     SessionManager       │
    │  MemoryStore         KnowledgeService      │  State Layer
    │  WorkspaceManager    EnvironmentMgr        │
    │  SemanticRecallProvider  MessageRepository │
    └───┬───────────────────────────────────────┘
    ┌───┴───────────────────────────────────────┐
    │  SQLiteAdapter  ForgeLLMAdapter           │  Adapter Layer
    │  ExportService  ToolGateway               │
    └───┬───────────────────────────────────────┘
    ┌───┴───────────────────────────────────────┐
    │  SQLite DB  ·  Filesystem  ·  Docker      │  Persistence
    └───────────────────────────────────────────┘
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
