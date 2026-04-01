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

## Taxonomia de Modos de Execucao (v0.4.0+)

> Evolucao do `tool_mode` original de 3 para 4 modos, refletindo
> a real diversidade de tarefas que agentes LLM enfrentam.
> Decisao tomada em 2026-04-01 apos analise de literatura.

### A distinção fundamental

```
tool_mode: Literal["instant", "brief", "long_run", "continuous"]
```

| Mode | Natureza | Tem fim? | Motivador | Duração | Exemplo |
|---|---|---|---|---|---|
| **Instant** | Pergunta → resposta | Sim, imediato | Pergunta | Segundos | "Capital de Buenos Aires?" |
| **Brief** | Tarefa composta | Sim, minutos | Tarefa | Minutos | "Liste clientes + email + WhatsApp" |
| **Long-run** | Projeto bounded | Sim, horas | **Objetivo** | Horas | "Construa um PDV completo" |
| **Continuous** | Agente always-on | **Nao** | **Proposito** | Indefinido | Assistente pessoal proativo |

**Long-run vs Continuous — a distincao filosofica:**
- Long-run tem **objetivo** — "construa X". Termina quando X esta pronto.
- Continuous tem **proposito** — "mantenha a redacao produtiva". Nunca termina.
  O continuous **gera seus proprios objetivos** derivados do proposito conforme
  o contexto muda. Quando ocioso, nao e erro — e oportunidade.

---

## Implementacao dos Modos (estado atual)

### Instant Mode (v0.3.1) — Implementado

Fast-path: single LLM call, scoring mode-aware, context seletivo (memory_share
capped 0.25, procedurais primeiro), evolucao per-mode, tuner filtering.

### Brief Mode (v0.3.2) — Implementado

Loop com trace sync completo, scoring calibrado para multi-step (<=3=1.0,
<=7=0.85, <=10=0.7), tool instructions com continuidade multi-step.

### Long-run Mode — Design

> Fontes que embasam este design:
> - Anthropic "Effective Harnesses for Long-Running Agents" (2025)
> - Anthropic "Harness Design for Long-Running Application Development" (2026)
> - Meta-Harness paper (Stanford/CMU, 2026) + Berman commentary
> - Hermes Agent analysis (`kb/2026-03-30_hermes-recommendations-for-symbiote.md`)
> - Ralph Loop research (`kb/ralph-loop-analysis.md`)
> - Claude Code source analysis (`~/dev/research/claude-code`)

#### Filosofia

O long-run e um **projeto** — tem inicio, planejamento, execucao em blocos,
verificacao em marcos, e entrega. Pode spanar multiplas sessoes ou um unico
context window longo. Diferente do brief (que acumula contexto e compacta),
o long-run pode optar por **context reset** entre blocos de trabalho.

A literatura converge em 3 pilares:

1. **Decompor antes de executar** — Planner expande prompt em spec/plano
2. **Verificar com agente separado** — Evaluator testa o que o Generator fez
3. **Estado persiste em artefatos, nao em mensagens** — handoff estruturado

#### Arquitetura: 3 fases modulares

```
┌──────────────────────────────────────────────────────────────┐
│                      LONG-RUN MODE                           │
│                                                              │
│  ┌──────────┐    ┌──────────────┐    ┌──────────────┐       │
│  │ PLANNER  │───>│  GENERATOR   │───>│  EVALUATOR   │       │
│  │          │    │              │    │              │       │
│  │ Expande  │    │ Executa em   │    │ Testa via    │       │
│  │ prompt   │    │ blocos de    │    │ tools e      │       │
│  │ em spec  │    │ trabalho     │    │ criterios    │       │
│  │ completo │    │ (sprints)    │    │ do host      │       │
│  └──────────┘    └──────┬───────┘    └──────┬───────┘       │
│                         │                    │               │
│                         │    feedback        │               │
│                         │<───────────────────┘               │
│                         │                                    │
│  Artefatos persistidos: │                                    │
│  ├─ plan.json (spec/plano do planner)                        │
│  ├─ progress.json (blocos feitos/pendentes)                  │
│  ├─ evaluation.json (feedback do evaluator)                  │
│  └─ handoff.json (estado para proxima sessao)                │
│                                                              │
│  Context strategy: compaction OU reset (configuravel)        │
│  Stop condition: completion_criteria OU max_iterations       │
│  Human checkpoints: a cada N blocos OU antes de alto risco   │
└──────────────────────────────────────────────────────────────┘
```

Cada fase e **opcional** — o host ativa o que precisa:
- Sem planner: o host fornece o spec diretamente
- Sem evaluator: o generator se auto-avalia (menos robusto, mais barato)
- Com evaluator: GAN-inspired — quem faz nao julga, quem julga nao faz

#### L-01: Planner Phase

**O que faz:** Recebe prompt curto (1-4 frases) e expande em spec completo com:
- Lista de blocos de trabalho (features/sprints)
- Criterios de sucesso por bloco
- Dependencias entre blocos
- Estimativa de complexidade

**Inspiracao:** Artigo Anthropic v2 — "I wanted to automate [the spec] step,
so I created a planner agent that took a simple 1-4 sentence prompt and
expanded it into a full product spec."

**Implementacao:**
- Prompt de planner customizavel pelo host (nao hardcoded para coding)
- Output persistido como artefato JSON (plan.json) — source of truth entre sessoes
- O planner roda como chamada LLM separada (sem tool loop) antes do generator iniciar
- Host pode fornecer criterios de avaliacao que o planner incorpora ao spec

**Exemplos por dominio:**
- Coding: "Construa um PDV" → spec com 16 features em 10 sprints
- Jornalismo: "Cobertura completa do evento" → spec com apuracao, fontes, redacao, revisao
- Pesquisa: "Estado da arte em memorias de agentes" → spec com survey, taxonomia, analise, formatacao

#### L-02: Generator com blocos de trabalho

**O que faz:** Executa o plan bloco a bloco. Cada bloco e uma unidade de trabalho
que pode ser verificada independentemente.

**Antes de cada bloco — Sprint Contract:**
Generator e evaluator negociam o que "done" significa antes de comecar.
O generator propoe o que vai fazer e como verificar; o evaluator (ou o planner)
valida que os criterios sao adequados.

**Context strategy (configuravel por symbiote):**

| Estrategia | Quando usar | Como funciona |
|---|---|---|
| **Compaction** | Modelos fortes (Opus 4.6+) | Compaction agressiva (70% threshold), manter ultimas 10 mensagens |
| **Context reset** | Modelos com "context anxiety" | Limpar mensagens entre blocos, re-ler plan.json + progress.json |
| **Hybrid** | Default | Compaction dentro do bloco, reset entre blocos |

O artigo Anthropic v2 confirma: "Opus 4.6 largely removed [context anxiety],
so I was able to drop context resets entirely. The agents were run as one
continuous session with automatic compaction." Mas para outros modelos,
context reset continua essencial.

**Progresso persistido:** Apos cada bloco, atualizar progress.json com:
- Blocos completados (com timestamp e metricas)
- Bloco atual (em andamento ou proximo)
- Decisoes tecnicas tomadas
- Problemas encontrados

#### L-03: Evaluator Phase (GAN-inspired)

**O que faz:** Avalia o trabalho do generator com prompt/modelo separado.
LLMs sao pessimos em auto-avaliacao — "confidently praise the work, even when
quality is obviously mediocre" (Anthropic). Separar quem faz de quem julga e
muito mais tratavel do que fazer o generator ser autocritico.

**Inspiracao direta:** "Tuning a standalone evaluator to be skeptical turns
out to be far more tractable than making a generator critical of its own work."

**Implementacao:**
- Prompt de evaluator customizavel pelo host com **criterios graduaveis**
- Cada criterio tem nome, descricao, peso, e threshold minimo
- O evaluator roda apos cada bloco (ou apos N blocos, configuravel)
- Se algum criterio abaixo do threshold → bloco reprovado → feedback para generator
- O evaluator pode usar tools para verificar (ex: Playwright para testar UI)
- Opcionalmente usa LLM diferente (evaluator_llm separado, como o evolver_llm)

**Criterios graduaveis — framework generico:**
O host define os criterios relevantes para seu dominio:
- Coding: completude funcional, qualidade de design, testes, bugs
- Jornalismo: precisao factual, fontes verificadas, qualidade editorial, SEO
- Pesquisa: rigor metodologico, cobertura da literatura, originalidade, formatacao
- Atendimento: resolucao do problema, satisfacao, tempo de resolucao

**Quando o evaluator NAO vale a pena:**
"The evaluator is not a fixed yes-or-no decision. It is worth the cost when
the task sits beyond what the current model does reliably solo." (Anthropic)
Para tarefas dentro da capability do modelo, o evaluator e overhead.
O host decide quando ativar.

#### L-04: Handoff entre sessoes

**O que faz:** Quando uma sessao long-run precisa ser interrompida (context
window cheio, timeout, checkpoint humano), gera artefato de handoff para a
proxima sessao retomar.

**Handoff artifact (handoff.json):**
```json
{
  "session_id": "...",
  "plan_ref": "plan.json",
  "progress_ref": "progress.json",
  "current_block": 5,
  "total_blocks": 12,
  "last_action": "Completou implementacao do modulo de pagamentos",
  "next_action": "Iniciar modulo de relatorios (bloco 6)",
  "open_issues": ["Bug no calculo de impostos nao resolvido"],
  "decisions_made": ["Escolheu PostgreSQL por suportar JSON nativo"],
  "context_summary": "..."
}
```

**Na proxima sessao (orientation):**
- ContextAssembler detecta `is_session_start` + handoff disponivel
- Injeta bloco de orientacao: handoff + ultimas N sessoes resumidas
- Instrucao: "Voce esta retomando um projeto. Leia o estado antes de agir."

#### L-05: Scoring e observabilidade

**Scoring long-run:**
- Nao penalizar por iteracoes (muitas sao esperadas)
- Sinal principal: blocos completados / blocos totais (completion rate)
- Sinal secundario: evaluator scores por bloco
- Sinal terciario: feedback do host

**Observabilidade:**
- LoopTrace com tool_mode="long_run"
- Metricas por bloco: iteracoes, tool calls, elapsed_ms, evaluator score
- Custo acumulado (via LoopTrace.total_elapsed_ms + token counts)
- Progress tracking persistido (progress.json)

#### L-06: Configuracao por symbiote

Novos campos no EnvironmentConfig para long-run:
```python
# Planner
planner_prompt: str | None = None          # Prompt do planner (None = skip)
planner_llm: str | None = None             # LLM do planner (None = usar principal)

# Evaluator
evaluator_prompt: str | None = None        # Prompt do evaluator (None = skip)
evaluator_llm: str | None = None           # LLM do evaluator (None = usar principal)
evaluator_criteria: list[dict] | None = None  # Criterios graduaveis
evaluator_frequency: int = 1               # Avaliar a cada N blocos

# Context strategy
context_strategy: Literal["compaction", "reset", "hybrid"] = "hybrid"

# Human checkpoints
checkpoint_frequency: int = 0              # 0 = sem checkpoints automaticos
checkpoint_before_high_risk: bool = True    # Pausar antes de tools high-risk

# Completion
completion_criteria: str | None = None     # Completion promise (ex: "TESTS_PASSED")
max_blocks: int = 20                       # Max blocos de trabalho
```

#### Priorizacao de implementacao

| # | Item | Esforco | Impacto | Dependencia |
|---|---|---|---|---|
| L-02 | Generator com blocos + context strategy | Alto | Alto | Nenhuma |
| L-04 | Handoff entre sessoes | Medio | Alto | Nenhuma |
| L-01 | Planner phase | Medio | Alto | Nenhuma |
| L-03 | Evaluator phase | Alto | Alto | L-02 |
| L-05 | Scoring long-run | Baixo | Medio | L-02 |
| L-06 | Config por symbiote | Medio | Medio | L-01, L-03 |

---

### Continuous Mode (always-on) — Conceito

> Este modo e fundamentalmente diferente dos demais. Nao e uma tarefa
> com fim — e uma **entidade persistente** com proposito.

#### A distincao proposito vs objetivo

Long-run recebe **objetivos**: "construa X", "pesquise Y". Termina quando cumpre.

Continuous tem **proposito**: "mantenha a redacao produtiva", "apoie a pesquisa
do usuario", "monitore a saude do sistema". O proposito nao se cumpre — e uma
direcao permanente. O agente **gera seus proprios objetivos** derivados do
proposito conforme o contexto muda.

Quando ocioso, o agente nao para. Ele:
- Identifica oportunidades (matérias em draft há muito tempo, métricas anomalas)
- Gera objetivos ("sugerir publicacao da materia X", "alertar sobre metrica Y")
- Prioriza ("X e urgente, Y pode esperar")
- Age proativamente

#### Arquitetura conceitual

```
┌──────────────────────────────────────────────────────────────┐
│                    CONTINUOUS MODE                            │
│                                                              │
│  ┌──────────────────────────────────────────────────┐       │
│  │ PURPOSE ENGINE                                    │       │
│  │                                                    │       │
│  │ Proposito: "mantenha a redacao produtiva"          │       │
│  │                                                    │       │
│  │ ┌─────────────┐  ┌─────────────┐  ┌────────────┐ │       │
│  │ │ Goal        │  │ Priority    │  │ Initiative │ │       │
│  │ │ Generator   │  │ Queue       │  │ Engine     │ │       │
│  │ │             │  │             │  │            │ │       │
│  │ │ Deriva      │  │ Ordena por  │  │ Decide o   │ │       │
│  │ │ objetivos   │  │ urgencia +  │  │ que fazer  │ │       │
│  │ │ do contexto │  │ impacto     │  │ quando     │ │       │
│  │ │             │  │             │  │ ocioso     │ │       │
│  │ └─────────────┘  └─────────────┘  └────────────┘ │       │
│  └──────────────────────────────────────────────────┘       │
│                         │                                    │
│  Event sources:         │  Execution:                        │
│  ├─ User messages       │  ├─ Instant (queries simples)      │
│  ├─ Scheduled tasks     │  ├─ Brief (tarefas compostas)      │
│  ├─ Webhooks/triggers   │  └─ Long-run (projetos derivados)  │
│  ├─ Monitoring alerts   │                                    │
│  └─ Idle detection      │  O continuous ORQUESTRA os demais  │
│                         │  modos conforme a tarefa exige     │
└──────────────────────────────────────────────────────────────┘
```

**Insight arquitetural:** O continuous nao e um 4o modo de loop — e um
**orquestrador** que usa instant, brief e long-run conforme a tarefa exige.
Uma pergunta simples usa instant. Uma tarefa composta usa brief. Um projeto
derivado usa long-run. O continuous decide qual modo aplicar.

#### Componentes necessarios (futuro)

| Componente | O que faz | Inspiracao |
|---|---|---|
| **Purpose Engine** | Mantem o proposito e gera objetivos | Conceito proprio — nao encontrado na literatura |
| **Goal Generator** | Deriva objetivos concretos do proposito + contexto | Similar a planning mas continuo |
| **Priority Queue** | Ordena objetivos por urgencia e impacto | Hermes scheduler + monitoring |
| **Initiative Engine** | Decide o que fazer quando ocioso | Auto-Dream do Claude Code (consolidacao proativa) |
| **Event Loop** | Reage a triggers externos e internos | Cron scheduler do Claude Code |
| **Mode Selector** | Escolhe instant/brief/long_run por tarefa | Novo — baseado em complexidade estimada |

#### Quando implementar

O continuous depende de:
1. Long-run funcional (para delegar projetos)
2. Brief e instant estabilizados (para delegar tarefas)
3. Scheduling/cron no kernel (base do event loop)
4. Memory consolidation madura (para manter coerencia ao longo do tempo)

E o modo mais ambicioso e diferenciador do Symbiote — um kernel que sustenta
**entidades que vivem**. Mas precisa dos modos inferiores estabilizados primeiro.

---

## Itens de suporte (cross-mode)

Estes itens beneficiam multiplos modos e podem ser implementados
independentemente:

### S-01: Orientacao automatica na 1a mensagem

Qualquer modo se beneficia de contexto extra na primeira mensagem da sessao.
O `ContextAssembler` detecta `is_session_start` e injeta: ultimas N sessoes
resumidas (via SessionRecallPort), memorias procedurais relevantes, handoff
(se existir).

**Impacto:** Alto para long-run e continuous. Medio para brief.
**Esforco:** Baixo.

### S-02: Handoff note no close_session

Nova `MemoryCategory.handoff` — resumo de continuidade gerado no `close_session()`,
separado da reflection. Foco em: o que estava fazendo, onde parou, proximo passo.

**Impacto:** Alto para long-run (multi-sessao). Medio para brief.
**Esforco:** Medio.

### S-03: Evaluator injection (GAN-inspired)

Framework generico para injetar uma fase de avaliacao com prompt/modelo separado.
Nao e self-verification (o mesmo agente se avalia) — e **avaliacao cruzada**
(agente diferente avalia). Mais robusto porque "tuning a standalone evaluator
to be skeptical is far more tractable than making a generator critical of its
own work." (Anthropic)

**Impacto:** Alto para long-run. Medio para brief (tarefas complexas).
**Esforco:** Alto.

### S-04: Criterios graduaveis definidos pelo host

Framework para o host definir criterios de avaliacao com nome, descricao,
peso e threshold. Transforma julgamentos subjetivos em gradacoes concretas.
Usado pelo evaluator (S-03) e pelo scoring.

**Impacto:** Alto para long-run. Medio para brief.
**Esforco:** Medio.

### S-05: Cost awareness no kernel

Para long-run, o custo total pode ser significativo ($125-200 por sessao
segundo benchmarks Anthropic). O kernel deve ter awareness de budget:
campo `max_cost_usd` no EnvironmentConfig, tracking via LoopTrace, pausa
quando budget se aproxima.

**Impacto:** Alto para long-run. Baixo para instant/brief.
**Esforco:** Medio (depende de token counting no LoopTrace).

---

## Itens NAO implementados (escopo externo)

| Item | Razao |
|---|---|
| **B-36: Cost tracking detalhado** | Pertence ao SymGateway. Kernel tem awareness, gateway mede |
| **B-41: Kimi K2 context limit** | Problema do provider (Groq), nao do kernel |
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

6. **4 modos em vez de 3:** O `tool_mode` original (instant/brief/continuous) foi expandido para 4 modos (instant/brief/long_run/continuous) apos perceber que "long-run" (projeto com fim) e "continuous" (agente always-on) sao conceitos fundamentalmente diferentes. Long-run tem objetivo, continuous tem proposito.

7. **Claude Code e mais simples que os artigos:** Analise do source revelou que o produto real nao implementa planner/evaluator automaticos — os artigos descrevem experimentos de pesquisa. Decisao: implementar o sofisticado, nao copiar o simples.

### Inspiracoes externas

- **Meta-Harness paper (Stanford/CMU, 2026):** Filesystem-based execution traces, self-evolving harness, feedback signals automaticos. Base conceitual para toda a implementacao.
- **Berman commentary:** "Bitter lesson" aplicada a harnesses — pare de hardcodar, deixe o sistema aprender.
- **Anthropic "Effective Harnesses" (2025):** Context management, progress files, incremental progress, handoff.
- **Anthropic "Harness Design for Long-Running Apps" (2026):** Planner/Generator/Evaluator, sprint contracts, criterios graduaveis, GAN-inspired separation, context reset vs compaction.
- **Ralph Loop research:** Fresh context philosophy, completion promise, context rot como inimigo. Implementacao descartada (bash loop), filosofia absorvida.
- **Hermes Agent:** Session recall, procedural memory, compressao de contexto preservando decisoes.
- **Claude Code source:** Auto-dream (consolidacao proativa), cron scheduler (scheduled tasks), coordinator mode (orquestracao), compaction simples (90% threshold, keep 10 recent). Produto prioriza simplicidade; nos priorizamos sofisticacao.
