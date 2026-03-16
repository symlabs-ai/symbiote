# Task List — Symbiote

> Ciclo: cycle-01
> Derivado de: project/docs/PRD.md
> Data: 2026-03-16

<!-- Prioridades aprovadas pelo stakeholder em 2026-03-16 -->

---

## Tasks

### Sequência de Sprints

| Sprint | Objetivo | Tasks | Escopo | Gate de saída |
|--------|----------|-------|--------|---------------|
| sprint-01 | Fundação: config, persistência, modelos base, identity | T-01, T-02, T-03, T-04 | current_cycle | Sprint Expert Gate (`/ask fast-track`) |
| sprint-02 | Sessões e workspace: ciclo de vida + artefatos | T-05, T-06, T-07 | current_cycle | Sprint Expert Gate (`/ask fast-track`) |
| sprint-03 | Memória: 4 camadas + knowledge separado | T-08, T-09, T-10 | current_cycle | Sprint Expert Gate (`/ask fast-track`) |
| sprint-04 | Context assembly + environment/policy | T-11, T-12, T-13 | current_cycle | Sprint Expert Gate (`/ask fast-track`) |
| sprint-05 | Runners + process engine + tools | T-14, T-15, T-16, T-17 | current_cycle | Sprint Expert Gate (`/ask fast-track`) |
| sprint-06 | Capacidades (Learn/Teach/Chat/Work/Show/Reflect) + reflexão | T-18, T-19 | current_cycle | Sprint Expert Gate (`/ask fast-track`) |
| sprint-07 | Interfaces: CLI + HTTP + export | T-20, T-21, T-22 | current_cycle | Sprint Expert Gate (`/ask fast-track`) |
| sprint-08 | Integração ponta-a-ponta + cenário MVP completo | T-23, T-24 | current_cycle | Sprint Expert Gate (`/ask fast-track`) |

---

### Sprint 1 — Fundação

Objetivo: Estabelecer config, persistência SQLite, modelos Pydantic base e IdentityManager. Sem isso, nada mais funciona.

| ID | Task | From US | Value Track | Priority | Size | Status | BlockedBy |
|----|------|---------|-------------|----------|------|--------|-----------|
| T-01 | Criar módulo `config/models.py` — modelo Pydantic de configuração global do kernel (db_path, context_budget, llm_provider, log_level) | US-14 | persistence_integrity | P0 | S | pending | — |
| T-02 | Criar adapter SQLite (`adapters/storage/sqlite.py`) — conexão, init schema, migrações básicas. Tabelas: symbiotes, sessions, messages, memory_entries, workspaces, artifacts, environment_configs, decisions, process_instances | US-01, US-02, US-06 | persistence_integrity | P0 | L | pending | T-01 |
| T-03 | Criar modelos Pydantic de domínio (`core/models.py`) — Symbiote, Session, Message, MemoryEntry, Workspace, Artifact, EnvironmentConfig, Decision, ProcessInstance | US-01 a US-14 | persistence_integrity | P0 | M | pending | T-01 |
| T-04 | Implementar IdentityManager (`core/identity.py`) — create, get, update persona. Persistência via adapter SQLite. Auditoria de alterações de persona | US-01 | learn, chat | P0 | M | pending | T-02, T-03 |

---

### Sprint 2 — Sessões e Workspace

Objetivo: Ciclo de vida de sessões (criar, retomar, encerrar com summary) e workspace com artefatos reais no filesystem.

| ID | Task | From US | Value Track | Priority | Size | Status | BlockedBy |
|----|------|---------|-------------|----------|------|--------|-----------|
| T-05 | Implementar SessionManager (`core/session.py`) — start, resume, close, add_message, add_decision. Gerar summary no close (placeholder: últimas N mensagens concatenadas) | US-02 | session_lifecycle, chat | P0 | M | pending | T-02, T-03 |
| T-06 | Implementar WorkspaceManager (`workspace/manager.py`) — create workspace, set workdir, track active files | US-03 | work | P0 | M | pending | T-02, T-03 |
| T-07 | Implementar ArtifactManager (`workspace/artifacts.py`) — register artifact (path, type, description), list by session/workspace, verify file exists on disk | US-03 | work | P0 | S | pending | T-06 |

---

### Sprint 3 — Memória e Knowledge

Objetivo: Memory stack em 4 camadas com persistência e ranking. Knowledge como serviço separado.

| ID | Task | From US | Value Track | Priority | Size | Status | BlockedBy |
|----|------|---------|-------------|----------|------|--------|-----------|
| T-08 | Implementar MemoryStore (`memory/store.py`) — store entry, search by query/scope/tags, get_relevant por ranking (escopo + importância + recência). Tipos: working, session_summary, relational, preference, constraint, factual, procedural, decision, reflection, semantic_note | US-06 | learn, reflect | P0 | L | pending | T-02, T-03 |
| T-09 | Implementar KnowledgeService (`knowledge/service.py`) — register source, query by theme. Tabela/store separada de memory. Retorno identificável por origem | US-05 | teach, chat | P0 | M | pending | T-02, T-03 |
| T-10 | Implementar WorkingMemory helper (`memory/working.py`) — manter estado operacional imediato (últimas mensagens, objetivo atual, plano ativo, decisões recentes, arquivos ativos). Atualizar automaticamente ao receber mensagens | US-06 | chat | P0 | S | pending | T-05, T-08 |

---

### Sprint 4 — Context Assembly e Environment

Objetivo: Pipeline de montagem de contexto seletivo com orçamento. Environment com tools e policies.

| ID | Task | From US | Value Track | Priority | Size | Status | BlockedBy |
|----|------|---------|-------------|----------|------|--------|-----------|
| T-11 | Implementar ContextAssembler (`core/context.py`) — pipeline: load identity → load session → recover memories → recover knowledge → rank → compress → assemble. Orçamento configurável. Inspeção do contexto montado | US-07 | context_assembly | P0 | L | pending | T-04, T-05, T-08, T-09 |
| T-12 | Implementar EnvironmentManager (`environment/manager.py`) — register tools, services, humans, policies. Configuração por simbióta e por workspace | US-04 | policy_enforcement | P0 | M | pending | T-02, T-03 |
| T-13 | Implementar PolicyGate (`environment/policies.py`) — check authorization antes de executar tool. Log de bloqueios. Tools ativadas/desativadas por config | US-04, US-09 | policy_enforcement | P0 | M | pending | T-12 |

---

### Sprint 5 — Runners, Process Engine e Tools

Objetivo: Runners especializados, process engine declarativo e tool gateway com policy gate.

| ID | Task | From US | Value Track | Priority | Size | Status | BlockedBy |
|----|------|---------|-------------|----------|------|--------|-----------|
| T-14 | Implementar Runner base + registry (`runners/base.py`) — interface Runner (can_handle, run), RunnerRegistry (register, select by intent) | US-08 | chat, work | P0 | S | pending | T-11 |
| T-15 | Implementar ChatRunner (`runners/chat.py`) — recebe contexto montado, chama LLM adapter, retorna resposta, atualiza working memory | US-08, US-11 | chat | P0 | M | pending | T-14, T-10 |
| T-16 | Implementar ToolGateway (`environment/tools.py`) — executa tools (fs read/write, list dir, search) respeitando PolicyGate. Log de auditoria com timestamp, tool_id, params, result | US-09 | work, policy_enforcement | P0 | M | pending | T-13 |
| T-17 | Implementar ProcessEngine (`process/engine.py`) — definição declarativa de processo (name, steps, entry criteria, checkpoints, outputs, reflection policy). ProcessRunner que executa steps em sequência e persiste checkpoints | US-10 | work, reflect | P0 | L | pending | T-14, T-16 |

---

### Sprint 6 — Capacidades e Reflexão

Objetivo: As 6 capacidades do simbióta como operações explícitas + ReflectionEngine.

| ID | Task | From US | Value Track | Priority | Size | Status | BlockedBy |
|----|------|---------|-------------|----------|------|--------|-----------|
| T-18 | Implementar ReflectionEngine (`core/reflection.py`) — executar ao close_session e após processos: gerar summary, extrair fatos duráveis (preferência, procedimento, restrição), descartar ruído, persistir candidatos a memória via MemoryStore | US-12 | reflect, learn | P0 | L | pending | T-05, T-08 |
| T-19 | Implementar CapabilitySurface (`core/capabilities.py`) — expor Learn, Teach, Chat, Work, Show, Reflect como métodos do kernel que orquestram os componentes corretos (MemoryStore, ContextAssembler, Runners, ReflectionEngine, export) | US-11 | learn, teach, chat, work, show, reflect | P0 | L | pending | T-11, T-14, T-15, T-16, T-17, T-18 |

---

### Sprint 7 — Interfaces e Export

Objetivo: CLI funcional, API HTTP mínima e export Markdown.

| ID | Task | From US | Value Track | Priority | Size | Status | BlockedBy |
|----|------|---------|-------------|----------|------|--------|-----------|
| T-20 | Implementar ExportService (`adapters/export/markdown.py`) — export session summary, long-term memory e decision log em Markdown legível | US-13 | show | P0 | M | pending | T-05, T-08 |
| T-21 | Implementar CLI completa (`cli/main.py`) — comandos: `symbiote create`, `session start/resume/close`, `message`, `memory search`, `export`. Usa SymbioteKernel | US-14 | chat, work, show | P0 | L | pending | T-19, T-20 |
| T-22 | Implementar API HTTP (`api/http.py`) — endpoints FastAPI: POST /symbiotes, GET /symbiotes/{id}, POST /sessions, POST /sessions/{id}/messages, POST /sessions/{id}/close, GET /memory/search. Usa SymbioteKernel | US-14 | chat, work, show | P0 | L | pending | T-19, T-20 |

---

### Sprint 8 — Integração e MVP

Objetivo: SymbioteKernel como orquestrador central + cenário ponta-a-ponta completo.

| ID | Task | From US | Value Track | Priority | Size | Status | BlockedBy |
|----|------|---------|-------------|----------|------|--------|-----------|
| T-23 | Implementar SymbioteKernel (`core/kernel.py`) — orquestrador central: create_symbiote, start_session, message, close_session. Compõe IdentityManager, SessionManager, MemoryStore, KnowledgeService, WorkspaceManager, EnvironmentManager, ContextAssembler, RunnerRegistry, ReflectionEngine, ExportService | US-01 a US-14 | todos | P0 | L | pending | T-19, T-20 |
| T-24 | Implementar LLMAdapter (`adapters/llm/base.py` + `adapters/llm/forge.py`) — interface abstrata para providers de LLM. Implementação ForgeLLM como default. Fallback mock para testes sem API key | US-08, US-11 | chat, teach | P0 | M | pending | T-01 |

---

### Legenda

**Priority**: P0 (must-have MVP) | P1 (should-have) | P2 (nice-to-have)

**Size**: XS (< 30min) | S (30min-2h) | M (2h-4h) | L (4h+)

**Status**: pending | in_progress | done | skipped

**BlockedBy**: IDs de tasks pré-requisito (ex: `T-01, T-03`) ou `—` se nenhuma dependência.

---

## Notas

### Dependências críticas

- **T-02 (SQLite adapter)** é o alicerce — quase tudo depende dele.
- **T-11 (ContextAssembler)** é o gargalo do meio — depende de identity, session, memory e knowledge.
- **T-19 (CapabilitySurface)** integra tudo — depende de todos os componentes de sprint 1-5.
- **T-24 (LLMAdapter)** pode ser feita em paralelo desde a sprint 1, mas só é consumida a partir do ChatRunner (T-15).

### Sprint Expert Gate

Ao concluir todas as tasks de uma sprint:

1. O `ft_manager` chama `/ask fast-track` com o contexto da sprint concluída.
2. O feedback é salvo em `project/docs/sprint-review-sprint-XX.md`.
3. Todas as recomendações do especialista viram correções obrigatórias dentro da sprint atual.
4. A próxima sprint só pode começar depois que o feedback estiver integralmente tratado.

### Paralelização

Quando `parallel_mode: true` no `ft_state.yml`, tasks em Value Tracks diferentes e sem `BlockedBy`
mútuo podem ser executadas em paralelo pelo ft_manager (via git worktrees).

- Tasks no **mesmo Value Track + mesma entidade** NÃO paralelizam.
- Tasks com **dependência de contrato** (port/interface compartilhada) NÃO paralelizam.
- Duas tasks **Size L** NÃO paralelizam simultaneamente.
- O forge_coder avalia independência técnica em `ft.tdd.01.selecao` e recomenda PARALELO ou SEQUENCIAL.
- Paralelização nunca atravessa duas sprints ao mesmo tempo.
