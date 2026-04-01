# Meta-Harness × Symbiote — Análise e Oportunidades

> Fonte: [Meta-Harness: End-to-End Optimization of Model Harnesses](https://yoonholee.com/meta-harness/) — Yoonho Lee, Roshen Nair, Qizheng Zhang, Kangwook Lee, Omar Khattab, Chelsea Finn (Stanford/CMU, 2026)
> Paper: [arXiv 2603.28052](https://arxiv.org/abs/2603.28052)
> Code: [GitHub](https://github.com/stanford-iris-lab/meta-harness-tbench2-artifact)
> Análise feita em: 2026-04-01

---

## O que o paper propõe

**Meta-Harness** define "harness" como **todo o código que determina o que armazenar, recuperar e apresentar ao LLM** — não o modelo em si, mas a infraestrutura ao redor. O insight central:

> Harnesses são desenhados à mão. Otimizadores de texto existentes comprimem feedback demais. Meta-Harness dá ao otimizador **acesso ao filesystem completo** com código-fonte, scores e execution traces de todas as tentativas anteriores — até **10M tokens de contexto diagnóstico por iteração** vs. ~26K dos métodos anteriores.

O loop: (1) agente lê filesystem com traces/scores/código de candidatos anteriores → (2) propõe novo harness → (3) avalia em tasks held-out → (4) logs vão pro filesystem → repete.

### Resultados

- +7.7 pontos em text classification (4x menos tokens que ACE)
- +4.7 em math reasoning (IMO-level, transfere para 5 modelos não vistos)
- #2 em TerminalBench-2 com Opus 4.6 (76.4%), #1 com Haiku 4.5 (37.6%)

### Comparativo de contexto por método

| Method | History | Log content | Mtok/iter |
|--------|---------|-------------|-----------|
| Self-Refine | Last | output + self-critique | 0.001 |
| OPRO | Window | (solution, score) pairs | 0.002 |
| TextGrad | Last | LLM textual gradient | 0.015 |
| MIPRO | Summary | bootstrapped program traces | 0.003 |
| AlphaEvolve | Window | program database + scores | 0.022 |
| GEPA | Summary | rollout traces (reasoning + tools) | 0.008 |
| Feedback Descent | Summary | pairwise comparison + feedback | 0.012 |
| TTT-Discover | Window | prev. solution fragment | 0.026 |
| **Meta-Harness** | **Full** | **all logs and scores** | **10.0** |

A diferença chave: 10M tokens de traces acessíveis via filesystem em vez de summaries comprimidos.

---

## Mapeamento Meta-Harness → Symbiote

O Symbiote **é** um harness no vocabulário do paper:

| Meta-Harness concept | Symbiote equivalent |
|---------------------|---------------------|
| **What to store** | MemoryStore, WorkingMemory, MemoryConsolidator |
| **What to retrieve** | ContextAssembler (ranked retrieval + budget) |
| **What to present** | `_build_system()`, `_build_messages()`, tool instructions |
| **Execution traces** | LoopTrace, audit_log, tool_results |
| **Harness code** | ChatRunner, LoopController, compaction layers |

---

## 5 Oportunidades Concretas

### 1. Self-Optimizing Context Assembly

**Problema**: O `ContextAssembler` usa splits fixos (40% memories, 25% knowledge) e heurística de tokens (chars/4). Isso é o harness hand-designed que o paper critica.

**Inspiração**: Meta-Harness descobre que para classificação com 215 classes, a melhor estratégia é "Label-Primed Query" — algo que nenhum humano desenharia. O split ideal depende do task.

**Implementação**: Adicionar um `ContextPolicy` por symbiote que o kernel ajusta com base nos `LoopTrace` de sessões anteriores. Se um symbiote nunca usa knowledge mas sempre precisa de mais memories, ajustar os splits automaticamente. Dados para isso já existem no audit_log + LoopTrace.

### 2. Execution Trace Filesystem (o diferencial do paper)

**Problema**: Hoje o `LoopTrace` é retornado no `RunResult` e logado, mas não persiste de forma queryable. Na próxima sessão, o symbiote não tem acesso aos traces anteriores.

**Inspiração**: A diferença chave do Meta-Harness vs. todos os baselines é: 10M tokens de traces acessíveis via filesystem em vez de summaries comprimidos. O proposer usa `grep` e `cat` para investigar falhas específicas.

**Implementação**: Persistir `LoopTrace` + tool results no SQLite (tabela `execution_traces`). No `ContextAssembler.build()`, quando o user_input é similar a uma sessão anterior que falhou, injetar um resumo do trace de falha como context: "Em sessão anterior, a tool X falhou 3x com erro Y. Tente abordagem diferente." Isso dá ao LLM **memória de execuções passadas**, não só memória de fatos.

### 3. Harness Variant Testing (a ideia mais poderosa)

**Problema**: O ChatRunner tem um único `_build_system()`, um único formato de tool instructions, um único padrão de compaction. Funciona ou não.

**Inspiração**: Meta-Harness testa 40 variantes de harness e mantém a melhor. No agentic coding, variações no system prompt e context management fizeram diferença de 28.5% → 46.5%.

**Implementação**: Não precisa de evolução completa. Algo mais simples:
- Manter 2-3 variantes de `_TOOL_INSTRUCTIONS` (conciso vs. detalhado vs. structured)
- Na primeira iteração do loop, usar a variante que teve melhor hit rate histórico para aquele modelo/provider
- Persistir `(provider, tool_instructions_variant, success_rate)` no config
- Isso é um A/B test interno do harness, automático

### 4. Counterfactual Diagnosis on Failure

**Problema**: Quando uma sessão falha (tool loop esgotado, circuit breaker, stagnation), o Symbiote hoje só faz `ReflectionEngine.reflect_session()` que extrai fatos genéricos.

**Inspiração**: O proposer do Meta-Harness faz **counterfactual diagnosis**: lê os traces e identifica "a falha no task X foi porque o harness não incluiu Y no prompt" — diagnóstico causal, não correlacional.

**Implementação**: No `close_session()`, se o `LoopTrace.stop_reason` não é `end_turn` (ou seja, houve stagnation/circuit_breaker/max_iterations), rodar uma reflexão especializada:

```
"A sessão falhou com stop_reason={reason}. Tools chamadas: {trace_summary}.
Qual decisão do harness causou a falha? O que deveria mudar?"
```

Persistir como `MemoryEntry(type="procedural", scope="global")` para que **todas** as sessões futuras desse symbiote aprendam com a falha.

### 5. Token Budget Awareness Adaptativo

**Problema**: O `context_budget` é fixo (default 16000) e o autocompact usa threshold fixo (80%).

**Inspiração**: Meta-Harness usa 4x menos tokens que ACE e consegue +7.7 pontos. Menos contexto != pior resultado. O que importa é a **qualidade** do que entra no contexto, não a quantidade.

**Implementação**:
- Após cada sessão, calcular `useful_context_ratio = tokens_that_led_to_success / total_tokens`
- Se o ratio é baixo (muita memória/knowledge injetada mas não usada), reduzir o budget para aquele symbiote
- Se o ratio é alto, manter ou aumentar
- Isso é o equivalente do paper: a evolução encontra harnesses que usam menos tokens mas são mais seletivos

---

## Priorização

| # | Oportunidade | Esforço | Impacto | Dados necessários |
|---|-------------|---------|---------|-------------------|
| 1 | Execution Trace Persistence | Médio | Alto | Já tem LoopTrace + audit_log |
| 2 | Counterfactual Diagnosis on Failure | Baixo | Alto | Já tem ReflectionEngine + LoopTrace |
| 3 | Self-Optimizing Context Splits | Médio | Médio | Precisa tracking de uso |
| 4 | Harness Variant Testing | Alto | Alto | Precisa A/B framework |
| 5 | Token Budget Adaptativo | Baixo | Médio | Já tem LoopTrace |

---

## Mensagem Central

O harness importa tanto quanto o modelo, e deve ser tratado como código otimizável, não como infraestrutura fixa. O Symbiote já tem os dados (traces, audit log, memory) — falta fechar o loop de feedback onde esses dados informam o próprio harness.
