# Harness Evolution Plan — Symbiote

> Documento vivo de planejamento para evolucao do harness agentico do Symbiote.
> Criado: 2026-04-01
> Ultima atualizacao: 2026-04-01
> Status: Fases 1-4 implementadas (v0.3.0). Trabalho futuro documentado.
> Referencias: `~/dev/kb/engenharia/meta_harness.md` (analise completa), `kb/meta-harness-analysis.md` (resumo no repo)

---

## Contexto

O Symbiote e um kernel embeddable para agentes LLM persistentes. No vocabulario do paper Meta-Harness (Stanford/CMU, 2026), o Symbiote **e** um harness — o codigo que determina o que armazenar, recuperar e apresentar ao modelo.

O paper demonstra que mudar apenas o harness (sem mudar o modelo) produz **6x de diferenca** no mesmo benchmark. A implicacao: otimizar o harness automaticamente e tao ou mais valioso que trocar de modelo.

---

## Implementacao Realizada (v0.2.22 — v0.3.0)

### Base de Resiliencia (v0.2.22)

| Componente | O que faz | Arquivo | Backlog |
|---|---|---|---|
| **LoopController** | Detecta stagnation (2x mesma call) e circuit breaker (3 falhas). Injeta stop message | `runners/loop_control.py` | B-57 |
| **LoopTrace** | Registra cada iteracao: tool_id, params, success, error, elapsed_ms, stop_reason | `runners/base.py` | B-57 |
| **LLM Retry** | 3 retries com backoff 1s/2s/4s em erros transientes | `runners/chat.py` | B-56 |
| **Parallel Tools** | asyncio.gather (async) + ThreadPoolExecutor (sync, max 4 workers) | `environment/tools.py` | B-55 |
| **3-Layer Compaction** | L1: microcompact (trunca >2000 chars), L2: loop compact (resume pares antigos), L3: autocompact (80% budget) | `runners/chat.py` | B-58 |

### Fase 1 — Fundacoes (v0.2.24)

Todas implementadas. Feedback signal funcionando e dados fluindo.

| Item | O que faz | Arquivo | Status |
|---|---|---|---|
| **H-01: SessionScore** (B-60) | `compute_auto_score()` — score 0.0-1.0 a partir de stop_reason + iterations + failure_rate | `core/scoring.py` | Implementado |
| **H-02: FeedbackPort** (B-61) | Protocol para host reportar feedback. `kernel.report_feedback()` compoe auto_score * 0.6 + user_score * 0.4 | `core/ports.py`, `core/kernel.py` | Implementado |
| **H-03: MemoryEntry de falha** (B-62) | Memoria procedural deterministica quando stop_reason != end_turn. Zero LLM | `core/kernel.py` | Implementado |
| **H-04: Context splits** (B-63) | `memory_share` e `knowledge_share` configuraveis por symbiote no EnvironmentConfig | `core/context.py`, `environment/manager.py` | Implementado |
| **H-05: LoopTrace persistence** (B-66) | Tabela `execution_traces` com steps, timing, stop_reason. Persistido no `close_session()` | `adapters/storage/sqlite.py` | Implementado |

**Decisao tomada:** Opcao 2 para propagacao do trace — `kernel._last_trace` como state temporario entre `message()` e `close_session()`.

### Fase 2 — Evolucao Automatica (v0.2.25)

Implementada com ativacao tiered para funcionar com zero dados.

| Item | O que faz | Arquivo | Status |
|---|---|---|---|
| **H-06: harness_versions** (B-64) | Versionamento de textos evolvable por symbiote com rollback chain | `harness/versions.py`, `adapters/storage/sqlite.py` | Implementado |
| **H-07: ParameterTuner** (B-65) | Auto-calibracao tiered (Tier 0-3) com safety caps e logging | `harness/tuner.py` | Implementado |
| **H-08: max_iterations config** (B-32) | `max_tool_iterations` per symbiote via EnvironmentConfig, cap 50 | `core/models.py`, `environment/manager.py` | Implementado |

**Tiers de ativacao implementados:**
- Tier 0 (0 sessoes): defaults hardcoded, sem ajustes
- Tier 1 (5+ sessoes): ajustes safe only (max_iterations, compaction threshold)
- Tier 2 (20+ sessoes): ajustes estatisticos (memory/knowledge splits)
- Tier 3 (50+ sessoes): fine tuning completo

**Decisao tomada:** Removido pre-requisito de "200+ sessoes para iniciar". O sistema trabalha com zero dados e ativa gradualmente conforme coleta.

### Fase 3 — Prompt Evolution (v0.2.26)

Implementada. O harness evolui os textos que controlam o LLM.

| Item | O que faz | Arquivo | Status |
|---|---|---|---|
| **H-09: HarnessEvolver** (B-67) | LLM proposer analisa traces (failed vs successful) e propoe textos melhorados. Guard rails + auto-rollback | `harness/evolver.py` | Implementado |
| **H-10: Memory/Knowledge on-demand** (B-68) | `context_mode: packed/on_demand`. `search_memories`/`search_knowledge` como builtin tools | `environment/tools.py`, `core/context.py` | Implementado |

**Componentes evolvable (3 textos, apenas estes):**
- `tool_instructions` — regras de comportamento com tools
- `injection_stagnation` — mensagem quando stagnation detectado
- `injection_circuit_breaker` — mensagem quando circuit breaker dispara

**Textos NAO evolvable (10, e por que):**
- `_INDEX_INSTRUCTIONS` — fatos tecnicos (lista de tools)
- `_build_system()` structure — parser-dependent, quebraria se mudasse
- Compaction format — resumo tecnico, nao comportamental
- Tool result formatting — consistencia > otimizacao
- Persona/identity — controlado pelo host, nao pelo harness
- Error hints — fatos, nao instrucoes
- Runtime context strip — metadata efemera
- On-demand instruction — fato tecnico ("voce tem tools de busca")
- Subagent delegation — fato tecnico
- Security banners — intocavel

**Guard rails implementados:**
- Versao nova nao pode ter > 2x o tamanho da anterior
- Linhas contendo "CRITICAL" devem ser preservadas
- Se proposer retorna lixo (JSON, codigo), descarta
- Minimo 50 sessoes antes de aceitar/rejeitar
- Rollback automatico se `new_avg < old_avg - 0.05`

**Decisao tomada:** Opcao 3 para proposer LLM — aceita ambos. Host pode injetar LLM separado via `kernel.set_evolver_llm()`, default usa o LLM principal. O ContextAssembler resolve versoes ativas via `harness_versions`.

### Fase 4 — Horizonte (v0.2.27)

Implementada. Inicialmente planejada como "futuro distante", foi executada imediatamente.

| Item | O que faz | Arquivo | Status |
|---|---|---|---|
| **B-33: Timeout** | Per-tool (30s) + loop total (300s) configuraveis per symbiote | `environment/tools.py`, `runners/chat.py` | Implementado |
| **B-29: Human-in-the-loop** | `risk_level` no ToolDescriptor + `on_before_tool_call` approval callback | `environment/descriptors.py`, `runners/chat.py` | Implementado |
| **B-34: Index mode cache** | Loop-local schema cache, reduz iterations ~50% em index mode | `runners/chat.py` | Implementado |
| **B-35: Multi-model test matrix** | E2E infra com 3 cenarios x N modelos | `tests/e2e/test_multi_model.py` | Implementado |
| **B-40: Tool Mode** | `instant/brief/continuous` substitui `tool_loop: bool` | `core/models.py`, `runners/chat.py` | Implementado |
| **B-27: Streaming mid-loop** | `on_progress` + `on_stream` callbacks para visibilidade real-time | `runners/chat.py` | Implementado |
| **B-30: Working memory intermediaria** | Loop summary prepended na WorkingMemory | `runners/chat.py` | Implementado |
| **H-11: BenchmarkRunner** | Task grading: tool_called, param_match, custom | `harness/benchmark.py` | Implementado |
| **H-12: StructuralEvolver** | Pluggable strategy registry com proposal/apply | `harness/structural.py` | Implementado |
| **H-13: CrossSymbioteLearner** | Tool overlap detection + harness version transfer | `harness/cross_learning.py` | Implementado |

**Decisao tomada:** O usuario definiu que "horizon features sao imediatas, nao distantes" e que "terao 10 symbiotas testando nos proximos 2 meses". Tudo foi implementado.

---

## Metricas de Implementacao

| Metrica | Valor |
|---|---|
| Total de testes | 1184 (130+ novos para harness) |
| Arquivos novos | 6 (scoring.py, versions.py, tuner.py, evolver.py, benchmark.py, structural.py, cross_learning.py) |
| Arquivos de teste novos | 18 |
| Tabelas SQLite novas | 3 (execution_traces, session_scores, harness_versions) |
| Campos novos em EnvironmentConfig | 7 (memory_share, knowledge_share, max_tool_iterations, tool_call_timeout, loop_timeout, tool_mode, context_mode) |
| Versao final | v0.3.0 |

---

## Principios de Design (mantidos como referencia)

### 1. Feedback signal composto (3 camadas)

```
Sinal 1: "Conseguiu ou desistiu?"
  stop_reason     | Score
  end_turn        | 1.0  — LLM completou naturalmente
  None (sem loop) | 0.8  — resposta direta, ok
  stagnation      | 0.2  — repetiu mesma acao
  circuit_breaker | 0.1  — tool quebrou 3x
  max_iterations  | 0.0  — esgotou limite

Sinal 2: "Funcionou de primeira?"
  1 iter + end_turn     | 1.0
  2-3 iter + end_turn   | 0.7
  5+ iter + end_turn    | 0.4
  Qualquer + falha      | 0.0

Sinal 3: "Usuario qualificou?" (opcional, via host)
  Composicao: final = auto * 0.6 + user * 0.4
```

### 2. "Let the model decide what it needs"

Implementado via `context_mode: on_demand` + builtin tools `search_memories`/`search_knowledge`.

### 3. Evolucao em 3 niveis (menor risco primeiro)

```
Nivel 1: Parameter Tuning    — zero LLM, SQL puro, tiered activation
Nivel 2: Prompt Evolution     — LLM proposer offline, rollback automatico
Nivel 3: Structural Evolution — pluggable strategy registry, sandbox futuro
```

### 4. Rollback sempre

Toda mudanca automatica e versionada e revertivel. Default hardcoded e sempre o fallback.

### 5. Zero-data ready

O sistema trabalha com zero dados e melhora conforme coleta. Tiers de ativacao garantem que nenhum ajuste automatico acontece sem dados suficientes.

---

## Trabalho Futuro

> Insights extraidos do artigo "Effective Harnesses for Long-Running Agents" (Anthropic, 2025)
> e de gaps identificados durante a implementacao.
>
> O artigo da Anthropic descreve um harness para coding agents especificamente.
> O Symbiote e um kernel agnostico de dominio — as generalizacoes abaixo abstraem
> os padroes do artigo para aplicabilidade em qualquer host.

### F-01: Orientacao automatica na primeira mensagem da sessao

**Origem:** Artigo Anthropic — "initializer agent" vs "coding agent" com prompts diferentes.

**Generalizacao:** A primeira mensagem de uma sessao deveria receber contexto extra automaticamente. Hoje o `ContextAssembler` monta o mesmo contexto para toda mensagem. Um `is_session_start: bool` no AssembledContext permitiria injetar um bloco de orientacao: ultimas N sessoes resumidas (via SessionRecallPort), memorias procedurais relevantes, e instrucao de "antes de agir, entenda o estado atual".

**Impacto:** Alto. Resolve o problema generico de "agente comeca do zero" sem o host ter que microgerenciar o prompt da primeira mensagem.

**Esforco:** Baixo. Flag booleano + condicional no ContextAssembler.

### F-02: Handoff note estruturado (separado da reflection)

**Origem:** Artigo Anthropic — "progress file" atualizado ao final de cada sessao.

**Generalizacao:** Hoje o `close_session()` faz reflection (aprendizado) + scoring. Falta um artefato de **continuidade** — um resumo curto focado em: "o que estava fazendo, onde parou, o que a proxima sessao deve fazer primeiro". Isso e diferente da reflection, que e sobre aprender padroes.

**Implementacao proposta:**
- Nova `MemoryCategory.handoff` no enum existente
- Geracao automatica no `close_session()`, apos reflection, antes de fechar
- Template: `"Sessao encerrada. Estado: {status}. Ultimo trabalho: {summary}. Proximo passo sugerido: {next_step}."`
- O SessionRecallPort prioriza memorias handoff no inicio da proxima sessao (complementa F-01)

**Impacto:** Alto. Bridging entre sessoes e o problema central do artigo.

**Esforco:** Medio. Nova categoria + geracao no close_session + priorizacao no recall.

### F-03: Self-verification gate antes do end_turn

**Origem:** Artigo Anthropic — "Claude marks features as done without proper testing".

**Generalizacao:** Antes de aceitar `end_turn`, o LoopController poderia injetar uma mensagem de verificacao: "antes de encerrar, verifique se o resultado esta correto usando as ferramentas disponiveis". Nao e human-in-the-loop (que ja temos), e **self-audit pelo proprio agente**.

**Implementacao proposta:**
- Novo `injection_verification` como 4o texto evolvable no HarnessEvolver
- Mensagem default: "Antes de encerrar, verifique se a tarefa foi concluida corretamente."
- Ativacao condicional: so injeta se a sessao usou tools (evita overhead em respostas diretas)
- O HarnessEvolver pode evoluir este texto como os outros 3

**Impacto:** Alto. Diferente de tudo que temos. Reduz false positives (agente diz que fez, nao fez).

**Esforco:** Medio. Nova injection no LoopController + texto evolvable.

### F-04: Session phases explicitas

**Origem:** Artigo Anthropic — fluxo implicito de orientation -> work -> verification -> handoff.

**Generalizacao:** O kernel poderia ter um `SessionPhase` (orientation, working, verification, handoff) que o ContextAssembler usa para ajustar o que injeta. Na fase orientation, mais memoria e recall. Na fase handoff, instrucao de continuidade. Hoje a sessao e um fluxo livre.

**Impacto:** Medio. Melhora qualidade de sessoes longas.

**Esforco:** Medio-alto. Novo conceito no kernel, deteccao automatica de fase, ajuste no ContextAssembler.

**Dependencia:** F-01 e F-02 cobrem 80% do valor sem a complexidade de phases explicitas. Avaliar se phases sao necessarias apos F-01/F-02 estarem em producao.

### F-05: Scope control evolvable por sessao

**Origem:** Artigo Anthropic — "work on only one feature at a time".

**Generalizacao:** Um mecanismo para limitar o escopo por sessao. Poderia ser um `scope_instruction` no EnvironmentConfig que o ContextAssembler injeta, e que o HarnessEvolver aprende a calibrar com base no scoring (sessoes com escopo amplo demais tendem a ter scores piores).

**Impacto:** Medio. O `tool_instructions` ja pode fazer isso manualmente. A diferenca e que seria automaticamente evolvable.

**Esforco:** Baixo. Novo campo no EnvironmentConfig + texto no ContextAssembler.

### F-06: Heuristica "declared victory too early" no scoring

**Origem:** Artigo Anthropic — agente ve progresso e declara que terminou.

**Generalizacao:** O `compute_auto_score()` poderia penalizar sessoes com `stop_reason=end_turn` + muito poucas iteracoes quando o contexto sugere que havia mais trabalho. Dificil de implementar de forma generica sem conhecer o dominio.

**Implementacao proposta:** Sinal simples — se o host reportou feedback negativo (score < 0.3) E o auto_score era alto (> 0.7), registrar como "false positive" na memoria procedural. O evolver pode aprender com esses casos.

**Impacto:** Baixo-medio. Depende de feedback do host para ser util.

**Esforco:** Baixo. Condicional no `report_feedback()`.

### F-07: JSON estruturado para memorias procedurais

**Origem:** Artigo Anthropic — "JSON because the model is less likely to overwrite JSON".

**Generalizacao:** Memorias procedurais e handoff poderiam ser JSON estruturado em vez de texto livre, reduzindo corrupcao pelo LLM em sessoes on-demand.

**Impacto:** Baixo. Guideline para hosts, nao necessariamente mudanca no kernel.

**Esforco:** Baixo.

### Priorizacao sugerida

| # | Item | Esforco | Impacto | Quando |
|---|---|---|---|---|
| F-01 | Orientacao automatica 1a msg | Baixo | Alto | Proximo sprint |
| F-02 | Handoff note | Medio | Alto | Proximo sprint |
| F-03 | Self-verification gate | Medio | Alto | Proximo sprint |
| F-05 | Scope control evolvable | Baixo | Medio | Proximo sprint |
| F-06 | False positive detection | Baixo | Medio | Segundo sprint |
| F-04 | Session phases | Medio-alto | Medio | Avaliar apos F-01/F-02 |
| F-07 | JSON para memorias | Baixo | Baixo | Guideline |

---

## Itens NAO implementados (escopo externo)

| Item | Razao |
|---|---|
| **B-36: Cost tracking** | Pertence ao SymGateway, nao ao kernel. O gateway ja mede tokens in/out |
| **B-41: Kimi K2 context limit** | Problema do provider (Groq), nao do kernel. Monitoramento |
| **B-42: Tool descriptions** | Cross-repo (YouNews). Melhoria no OpenAPI do host |
| **B-44: Narracao intermediaria** | Cross-repo (YouNews). Validacao pos-deploy |
| **B-45: Test harness E2E** | Requer YouNews + SymGateway rodando. Baixa prioridade |

---

## Observacoes Historicas

### Decisoes de design relevantes

1. **Tiered activation vs threshold fixo:** Inicialmente o plano exigia 200+ sessoes para Fase 2 e 500+ para Fase 3. O usuario definiu que "o sistema deve funcionar com zero dados". Redesenhamos para tiers graduais (0/5/20/50 sessoes).

2. **Proposer LLM:** Tres opcoes foram analisadas: (1) LLM separado obrigatorio, (2) mesmo LLM, (3) aceitar ambos. Opcao 3 implementada — host pode injetar LLM separado, default usa o principal.

3. **Quais prompts evoluir:** Dos 13 textos enviados ao LLM, apenas 3 foram classificados como evolvable (instrucoes comportamentais). Os demais sao fatos tecnicos, formatos parser-dependent, ou conteudo controlado pelo host.

4. **Horizon features imediatas:** O plano original deixava H-11/H-12/H-13 como "futuro distante". O usuario redefiniu: "terao 10 symbiotas testando em 2 meses". Tudo foi implementado imediatamente.

5. **Propagacao do LoopTrace:** Opcao 2 escolhida (kernel._last_trace stateful) por ser simples e consistente com o design existente do kernel.

### Inspiracoes externas

- **Meta-Harness paper (Stanford/CMU, 2026):** Filesystem-based execution traces, self-evolving harness, feedback signals automaticos. Base conceitual para toda a implementacao.
- **Berman commentary:** "Bitter lesson" aplicada a harnesses — pare de hardcodar, deixe o sistema aprender. Motivou o design de evolucao automatica.
- **Anthropic "Effective Harnesses" (2025):** Padroes para agentes long-running — orientacao, progresso incremental, verificacao, handoff. Inspirou os itens F-01 a F-07 do trabalho futuro.
