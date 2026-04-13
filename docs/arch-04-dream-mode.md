# Symbiote — Dream Mode

O Dream Mode e um motor de ruminacao em background que consolida, poda e melhora as memorias do agente fora do ciclo de sessao. Funciona como sono REM — enquanto o agente nao esta atendendo, ele "sonha" sobre o que aprendeu e busca maneiras de ser melhor.

## Toggle

O Dream Mode e controlado por symbiote via `EnvironmentConfig`:

| Campo | Tipo | Default | Descricao |
|-------|------|---------|-----------|
| `dream_mode` | `off / light / full` | `off` | Nivel de ativacao |
| `dream_max_llm_calls` | `1-50` | `10` | Budget maximo de chamadas LLM por ciclo |
| `dream_min_sessions` | `1-100` | `5` | Sessoes fechadas necessarias para disparar |

- **off**: Nunca roda. Zero custo.
- **light**: Apenas fases deterministas (Prune + Reconcile). Zero chamadas LLM.
- **full**: Todas as 5 fases, com budget controlado pelo `BudgetTracker`.

## Arquitetura

```mermaid
flowchart TB
    subgraph Trigger
        CS[kernel.close_session] --> MD{dream_mode != off?}
        MD -- Nao --> FIM1[Nada acontece]
        MD -- Sim --> SD{should_dream?}
        SD -- "< min_sessions" --> FIM2[Aguarda mais sessoes]
        SD -- ">= min_sessions" --> DA[dream_async]
    end

    subgraph Background Thread
        DA --> |daemon thread| DE[DreamEngine.dream]
        DE --> CTX[DreamContext]
        CTX --> P1

        subgraph "Fases (sequenciais)"
            P1[PrunePhase<br/>deterministic]
            P2[ReconcilePhase<br/>deterministic]
            P3[GeneralizePhase<br/>LLM required]
            P4[MinePhase<br/>LLM required]
            P5[EvaluatePhase<br/>LLM required]

            P1 --> P2
            P2 --> P3
            P3 --> P4
            P4 --> P5
        end

        P5 --> DR[DreamReport]
        DR --> |INSERT| DB[(dream_reports)]
    end

    subgraph Budget Control
        BT[BudgetTracker<br/>max_calls=10]
        P3 -.-> |consume 1 per cluster| BT
        P4 -.-> |consume 1| BT
        P5 -.-> |consume 1 per session| BT
        BT -.-> |exhausted, skip| P4
        BT -.-> |exhausted, skip| P5
    end

    style P1 fill:#9f9,stroke:#333
    style P2 fill:#9f9,stroke:#333
    style P3 fill:#f9f,stroke:#333
    style P4 fill:#f9f,stroke:#333
    style P5 fill:#f9f,stroke:#333
```

## Fases em Detalhe

### Phase 1 — Prune (Deterministic)

Desativa memorias obsoletas baseado em uma formula de decay:

```
decay = days_since_last_used * (1 - importance)
```

Se `decay > 30`, a memoria e desativada (soft-delete). Memorias do tipo `constraint` e `handoff` sao protegidas — nunca sao podadas.

```mermaid
flowchart LR
    A[(memory_entries<br/>is_active=1)] --> B{Para cada entrada}
    B --> C{type = constraint<br/>ou handoff?}
    C -- Sim --> D[Protegida — skip]
    C -- Nao --> E["decay = days * (1 - importance)"]
    E --> F{decay > 30?}
    F -- Sim --> G[deactivate]
    F -- Nao --> H[Manter]
```

**Exemplos**:
- Memoria com importance=0.3, 60 dias sem uso: `60 * 0.7 = 42` → podada
- Memoria com importance=0.9, 60 dias sem uso: `60 * 0.1 = 6` → mantida
- Constraint com importance=0.1, 100 dias: protegida → mantida

### Phase 2 — Reconcile (Deterministic)

Detecta memorias conflitantes (mesmos tags, conteudo divergente) e resolve mantendo a de maior importance.

```mermaid
flowchart TB
    A[(Memorias ativas<br/>com 2+ tags)] --> B[Agrupar por<br/>tag overlap]
    B --> C{Para cada par}
    C --> D["tag_overlap = |A∩B| / |A∪B|"]
    D --> E{tag_overlap >= 0.6?}
    E -- Nao --> F[Nao e conflito]
    E -- Sim --> G["content_sim = token_overlap"]
    G --> H{content_sim < 0.3?}
    H -- Nao --> I["Conteudo similar<br/>(concordam) — skip"]
    H -- Sim --> J["CONFLITO!<br/>Tags parecidas,<br/>conteudo divergente"]
    J --> K[Deactivate menor importance]
    K --> L["Tag winner com<br/>'dream:reconciled'"]
```

### Phase 3 — Generalize (LLM)

Encontra clusters de 3+ memorias procedurais similares e pede ao LLM para criar uma abstracao de nivel mais alto.

```mermaid
sequenceDiagram
    participant P as GeneralizePhase
    participant MS as MemoryStore
    participant BT as BudgetTracker
    participant LLM

    P->>MS: get_by_type("procedural", limit=100)
    MS-->>P: [mem1, mem2, mem3, mem4, ...]

    P->>P: cluster by tag overlap (2+ common tags)

    loop Para cada cluster de 3+
        P->>BT: consume(1)
        alt budget OK
            P->>LLM: "Synthesize these procedures into one rule:"
            LLM-->>P: "For GUI apps, use nohup <app> &"
            P->>MS: store(new MemoryEntry)<br/>type=procedural<br/>source=inference<br/>tags=[dream:generalized]
        else budget exhausted
            Note over P: Skip remaining clusters
        end
    end
```

### Phase 4 — Mine (LLM)

Analisa `execution_traces` com falhas recorrentes e gera memorias procedurais para evitar padroes problematicos.

```mermaid
flowchart LR
    A[(execution_traces<br/>stop_reason != end_turn)] --> B[Agrupar por<br/>tool_id + failure]
    B --> C[Top 3 padroes<br/>de falha]
    C --> D[LLM: identifique<br/>regras para evitar]
    D --> E[MemoryEntries<br/>type=procedural<br/>tags=dream:failure_pattern]
```

### Phase 5 — Evaluate (LLM)

Rele sessoes com score baixo (< 0.5) e gera insights de auto-melhoria.

```mermaid
flowchart LR
    A[(session_scores<br/>final_score < 0.5)] --> B[Carregar mensagens<br/>da sessao]
    B --> C[LLM: o que deu errado?<br/>Como melhorar?]
    C --> D[MemoryEntries<br/>type=reflection<br/>tags=dream:self_review]
```

## DreamReport — Output

Cada ciclo de dream produz um `DreamReport` persistido na tabela `dream_reports`:

```mermaid
classDiagram
    class DreamReport {
        +id: str
        +symbiote_id: str
        +started_at: datetime
        +completed_at: datetime
        +dream_mode: light | full
        +dry_run: bool
        +total_llm_calls: int
        +max_llm_calls: int
        +phases: list~DreamPhaseResult~
    }

    class DreamPhaseResult {
        +phase: str
        +started_at: datetime
        +completed_at: datetime
        +actions_proposed: int
        +actions_applied: int
        +llm_calls_used: int
        +details: list~dict~
        +error: str | None
    }

    class BudgetTracker {
        -_max: int
        -_used: int
        +consume(n) bool
        +remaining: int
        +used: int
    }

    class DreamContext {
        +symbiote_id: str
        +storage: StoragePort
        +memory: MemoryStore
        +llm: LLMPort | None
        +budget: BudgetTracker
        +dry_run: bool
        +last_dream_at: datetime
    }

    DreamReport *-- DreamPhaseResult
    DreamContext --> BudgetTracker
    DreamContext --> DreamReport : produces
```

## Dry-Run Mode

O `dry_run=True` faz todas as fases **proporem** acoes sem **aplicar** nenhuma. Util para inspecionar o que o Dream Mode faria antes de ativar em producao.

```python
report = kernel.dream(symbiote_id, dry_run=True)
for phase in report.phases:
    print(f"{phase.phase}: {phase.actions_proposed} propostas, {phase.actions_applied} aplicadas")
    for detail in phase.details:
        print(f"  → {detail}")
```

## Integracao com o Ciclo de Vida

```mermaid
sequenceDiagram
    participant User
    participant Kernel
    participant Reflection
    participant Dream

    User->>Kernel: message("Abre o Typora")
    Kernel-->>User: "Pronto."

    User->>Kernel: message("Fecha o VS Code")
    Kernel-->>User: "Pronto."

    Note over User,Kernel: ... N sessoes ...

    User->>Kernel: close_session()
    Kernel->>Kernel: persist_score()
    Kernel->>Reflection: reflect_session()
    Reflection-->>Kernel: ReflectionResult (facts extraidos)
    Kernel->>Kernel: persist_handoff_memory()

    Kernel->>Kernel: _maybe_dream(symbiote_id)
    Note over Kernel: dream_mode=light, min_sessions=5

    alt >= 5 sessoes desde ultimo dream
        Kernel->>Dream: dream_async(symbiote_id, "light")
        Note over Dream: daemon thread em background
        Dream->>Dream: PrunePhase.run()
        Dream->>Dream: ReconcilePhase.run()
        Dream->>Dream: persist DreamReport
    else < 5 sessoes
        Note over Kernel: Aguarda mais sessoes
    end
```

## Notas

- O Dream Mode **nunca bloqueia** uma sessao ativa — roda em daemon thread.
- O `BudgetTracker` e a protecao contra custo descontrolado de API. Se o budget se esgota na Phase 3, as Phases 4 e 5 sao automaticamente puladas.
- Memorias criadas pelo Dream Mode usam `source="inference"` — distinguindo-as de memorias de usuario (`source="user"`) ou sistema (`source="system"`).
- O `should_dream()` verifica 3 condicoes: (1) dream_mode != off, (2) nenhum dream ativo pro mesmo symbiote, (3) >= min_sessions desde o ultimo dream.
