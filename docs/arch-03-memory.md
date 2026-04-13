# Symbiote — Memoria e Aprendizado

O Symbiote tem um sistema de memoria em camadas inspirado na neurociencia cognitiva: **memoria de trabalho** (curto prazo, dentro da sessao), **memoria de longo prazo** (persistida em SQLite), **consolidacao** (transferencia do curto para o longo prazo), e **reflexao** (extracao de conhecimento duravel pos-sessao).

## Taxonomia de Memorias

O `MemoryEntry` suporta 10 tipos, organizados em 5 categorias:

```mermaid
mindmap
    root((MemoryEntry))
        Ephemeral
            working
        Declarative
            factual
            preference
            constraint
            relational
        Procedural
            procedural
            decision
        Meta
            session_summary
            reflection
            semantic_note
        Handoff
            handoff
```

Cada memoria tem **importance** (0-1), **confidence** (0-1), **source** (user/system/reflection/inference), **scope** (global/session/project), e **tags** para clustering.

## Pipeline Completo de Aprendizado

```mermaid
flowchart TB
    subgraph Sessao Ativa
        A[User Input] --> B[ContextAssembler.build]
        B --> |injeta memorias relevantes| C[ChatRunner]
        C --> D[LLM + Tool Loop]
        D --> E[Response]
        E --> F[WorkingMemory.update_message]
        F --> G{tokens > threshold?}
        G -- Sim --> H[MemoryConsolidator]
        G -- Nao --> I[Proxima mensagem]
        H --> |background thread| J[LLM Summarization]
        J --> K[Persist MemoryEntries<br/>factual, preference,<br/>constraint, procedural]
        K --> I
    end

    subgraph Close Session
        L[kernel.close_session] --> M[compute_auto_score]
        M --> N[persist SessionScore]
        L --> O[generate_failure_memory<br/>se loop falhou]
        L --> P[ReflectionEngine.reflect_session]
        P --> Q["Keyword extraction:<br/>prefer - preference<br/>always/never - constraint<br/>procedure - procedural"]
        Q --> R[Persist MemoryEntries<br/>source=reflection]
        L --> S[persist_handoff_memory<br/>para resumption]
    end

    subgraph Recall
        T[Proxima sessao] --> U[ContextAssembler]
        U --> V[MemoryStore.get_relevant]
        V --> W[SemanticRecallProvider.recall]
        W --> X["score = overlap*0.5<br/>+ importance*0.3<br/>+ recency*0.2"]
        X --> Y[Top N memorias<br/>injetadas no contexto]
        Y --> Z[LLM recebe memorias<br/>relevantes no prompt]
    end

    subgraph Background
        AA[DreamEngine] --> |prune, reconcile,<br/>generalize, mine, evaluate| BB[Memorias melhoradas]
    end

    I --> L
    R --> T
    K --> T
    BB --> T

    style H fill:#f9f,stroke:#333
    style P fill:#f9f,stroke:#333
    style AA fill:#bbf,stroke:#333
```

## Memoria de Trabalho vs Longo Prazo

```mermaid
flowchart LR
    subgraph WorkingMemory
        direction TB
        WM1[recent_messages: list<br/>max 20, trimmed por turno]
        WM2[current_goal: str]
        WM3[active_plan: str]
        WM4[active_files: list]
        WM5["snapshot() - dict"]
    end

    subgraph MemoryStore
        direction TB
        MS1[(SQLite: memory_entries)]
        MS2[store / get / search]
        MS3["get_relevant - recall scoring"]
        MS4["get_by_type / get_by_category"]
        MS5["deactivate - soft delete"]
    end

    subgraph Consolidator
        direction TB
        MC1[token_threshold: 2000]
        MC2[keep_recent: 6 messages]
        MC3[LLM summarization prompt]
        MC4[daemon thread]
    end

    WorkingMemory -->|"overflow\n(tokens > 2000)"| Consolidator
    Consolidator -->|"persist\nMemoryEntries"| MemoryStore
    MemoryStore -->|"inject via\nContextAssembler"| WorkingMemory
```

## Recall Semantico

O `SemanticRecallProvider` usa um scoring baseado em 3 fatores:

```mermaid
flowchart TB
    Q[Query: user input] --> T["tokenize - keywords"]
    T --> C[Fetch candidates<br/>limit * 5 from DB]

    C --> S1["Keyword Overlap 50%<br/>query ∩ entry / query ∪ entry"]
    C --> S2["Importance Weight 30%<br/>entry.importance"]
    C --> S3["Recency Weight 20%<br/>decay over 30 days"]

    S1 --> SC[score = 0.5*overlap + 0.3*importance + 0.2*recency]
    S2 --> SC
    S3 --> SC

    SC --> F{score > 0.1?}
    F -- Sim --> R[Retorna top N]
    F -- Nao --> D[Descartada]

    R --> U[Atualiza last_used_at]
```

## Consolidacao — Fluxo Detalhado

```mermaid
sequenceDiagram
    participant CR as ChatRunner
    participant WM as WorkingMemory
    participant MC as MemoryConsolidator
    participant LLM
    participant MS as MemoryStore

    CR->>WM: update_message(assistant_response)
    CR->>MC: consolidate_if_needed(wm, symbiote_id)

    MC->>MC: estimate_tokens(wm)

    alt tokens > 2000 AND messages > 6
        MC->>MC: split: old_msgs | recent_msgs
        MC->>WM: trim to keep_recent=6
        Note over MC: Non-blocking trim

        MC->>MC: spawn daemon thread

        Note over MC,LLM: Background thread
        MC->>LLM: complete(CONSOLIDATION_PROMPT + old_msgs)
        LLM-->>MC: JSON array of facts

        loop para cada fact extraido
            MC->>MS: store(MemoryEntry)<br/>type=fact.type<br/>importance=fact.importance<br/>source="system"
        end
    else tokens <= threshold
        Note over MC: Nada a fazer
    end
```

## Reflexao — Extracao Pos-Sessao

A `ReflectionEngine` roda no `close_session()` e usa heuristicas de keywords para extrair conhecimento duravel:

```mermaid
flowchart LR
    subgraph Input
        M[Session Messages<br/>limit=50]
    end

    subgraph Filter
        M --> N{is_noise?}
        N -- "< 10 chars ou<br/>'ok','thanks','done'" --> D[Descartada]
        N -- Nao --> K[Keyword scan]
    end

    subgraph Extract
        K --> |"prefer"| P[preference<br/>importance=0.6]
        K --> |"always/never/rule"| C[constraint<br/>importance=0.8]
        K --> |"procedure/convention"| PR[procedural<br/>importance=0.6]
        K --> |"decided/chose"| DE[decision<br/>importance=0.6]
        K --> |nenhum keyword| F[factual<br/>importance=0.5]
    end

    subgraph Persist
        P --> MS[(MemoryStore)]
        C --> MS
        PR --> MS
        DE --> MS
        F --> MS
    end
```

## Context Assembly — Budget Aware

O `ContextAssembler` respeita um budget de tokens e distribui entre memorias e conhecimento:

```mermaid
pie title Distribuicao do Context Budget
    "Persona + System" : 15
    "Working Memory" : 20
    "Relevant Memories" : 40
    "Relevant Knowledge" : 25
```

No modo `instant`, a distribuicao muda para priorizar eficiencia:

```mermaid
pie title Context Budget — Instant Mode
    "Persona + System" : 15
    "Working Memory" : 30
    "Relevant Memories (procedural first)" : 25
    "Relevant Knowledge" : 10
    "User Input + Tools" : 20
```

## Notas

- **Memorias nunca sao deletadas** — usam soft-delete (`is_active=False`). O `DreamEngine.PrunePhase` e o unico mecanismo que desativa memorias obsoletas.
- **last_used_at** e atualizado automaticamente pelo `SemanticRecallProvider` a cada recall — memorias usadas frequentemente resistem ao decay.
- **Cross-Symbiote Learning** (`harness/cross_learning.py`) permite transferir melhorias de harness entre symbiotes com tool sets similares (Jaccard overlap > 0.5).
- O `ParameterTuner` ajusta automaticamente `max_tool_iterations` e `memory_share` baseado em dados historicos, com tiers de seguranca (0-3) que exigem mais sessoes para mudancas mais agressivas.
