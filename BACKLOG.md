# Backlog — Symbiote

> Popule via `/backlog <descrição>` em maintenance mode.

## Ideias

| # | Descrição | Origem | Prioridade | Status |
|---|-----------|--------|------------|--------|
| 7 | MCP Integration via forge-llm (McpToolProvider → ToolGateway → PolicyGate) | dev | baixa | pending |
| 23 | Deploy Hosted — porta, domínio, nginx, CI para Symbiote como serviço | dev | alta | pending |
| 24 | DiscoveredToolLoader — bridge entre discovered_tools aprovadas e ToolGateway: ao inicializar o kernel, carregar automaticamente as tools com status=approved do SQLite e registrá-las como HttpTool com method + url_template salvos pelo Discovery | dev | alta | pending |

## Implementadas

| # | Descrição | Implementada em | Versão |
|---|-----------|-----------------|--------|
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
