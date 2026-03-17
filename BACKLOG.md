# Backlog — Symbiote

> Popule via `/backlog <descrição>` em maintenance mode.

## Ideias

| # | Descrição | Origem | Prioridade | Status |
|---|-----------|--------|------------|--------|
| 7 | MCP Integration via forge-llm (McpToolProvider → ToolGateway → PolicyGate) | dev | baixa | pending |

## Implementadas

| # | Descrição | Implementada em | Versão |
|---|-----------|-----------------|--------|
| 14 | SSRF Protection — validação de IP + redirect guard em HTTP tools | 2026-03-17 | 0.2.1 |
| 15 | Untrusted Content Banner — anti-prompt-injection em respostas HTTP | 2026-03-17 | 0.2.1 |
| 18 | Tool Call Pair Consistency — trim alinhado a turn boundaries | 2026-03-17 | 0.2.1 |
| 17 | GenerationSettings — pass-through de temperature/max_tokens/reasoning_effort | 2026-03-17 | 0.2.1 |
| 16 | Async Memory Consolidation — trim imediato + LLM em background thread | 2026-03-17 | 0.2.1 |
| 8 | Tool Error Hints — hint de retry automático em tool results com erro | 2026-03-17 | 0.2.0 |
| 9 | Runtime Context Strip — metadata efêmera no prompt sem poluir histórico | 2026-03-17 | 0.2.0 |
| 3 | MessageRepository port para isolar SQL do ReflectionEngine | 2026-03-17 | 0.2.0 |
| 10 | Memory Consolidation — sumarização automática via LLM quando tokens excedem threshold | 2026-03-17 | 0.2.0 |
| 11 | Subagent Spawning — delegação de tarefas entre Symbiotas com tool set restrito | 2026-03-17 | 0.2.0 |
| 12 | MessageBus — fila async inbound/outbound para desacoplar channels do kernel | 2026-03-17 | 0.2.0 |
| 13 | Progressive Skills — skills como .md no workspace, carregamento lazy por demanda | 2026-03-17 | 0.2.0 |
| 4 | Semantic recall provider (keyword-based MVP implementation) | 2026-03-17 | 0.2.0 |
| 6 | ProcessEngine cache invalidation para multi-worker | 2026-03-17 | 0.2.0 |
| 2 | Interactive chat mode na CLI (loop input/output) | 2026-03-17 | 0.2.0 |
| 5 | Integração com LLM real testada ponta-a-ponta | 2026-03-17 | 0.2.0 |
| 1 | Docker container de referência para modo serviço | 2026-03-17 | 0.2.0 |
