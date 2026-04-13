# Symbiote — Modos de Execucao

O Symbiote suporta 4 modos de execucao (`tool_mode`), cada um otimizado para um tipo de tarefa diferente. O modo e definido por symbiote via `EnvironmentConfig.tool_mode` e pode ser `auto` (resolvido dinamicamente pelo `ContextAssembler`).

## Visao Geral dos Modos

| Modo | Iteracoes | Uso tipico | Latencia |
|------|-----------|------------|----------|
| `instant` | 1 (sem loop) | Comandos rapidos, fire-and-forget | ~1-3s |
| `brief` | ate 10 | Tarefas conversacionais com tools | ~3-30s |
| `long_run` | Planner → N blocos | Tarefas complexas multi-etapa | minutos |
| `continuous` | sem limite, dias | Agente persistente, opera continuamente | horas-dias |

## Fluxo de Execucao — Instant Mode

No modo `instant`, o ChatRunner faz **uma unica chamada LLM** seguida de **uma unica rodada de tools** (sem loop). Ideal para o OS Agent do SymTalk, onde o usuario pede "Abre o Typora" e a acao e imediata.

O hook `on_after_tool_result` permite que o caller (ex: SymTalk) decida se o resultado da tool encerra o fluxo sem chamar o LLM novamente.

```mermaid
sequenceDiagram
    participant Caller
    participant Kernel as SymbioteKernel
    participant Ctx as ContextAssembler
    participant CR as ChatRunner
    participant LLM
    participant TG as ToolGateway

    Caller->>Kernel: message(session_id, "Abre o Typora")
    Kernel->>Ctx: build(session_id, symbiote_id, input)
    Ctx-->>Kernel: AssembledContext(tool_mode="instant")
    Kernel->>CR: run(context)

    Note over CR: _run_instant()
    CR->>LLM: complete(messages)
    LLM-->>CR: tool_call bash nohup typora
    CR->>CR: parse_response - ToolCall
    CR->>TG: execute_tool_calls
    TG-->>CR: ToolCallResult success=true

    alt on_after_tool_result hook set
        CR->>CR: hook returns Pronto
        Note over CR: Hook retorna texto, skip LLM
    else no hook, all success
        Note over CR: Default skip LLM, return Pronto
    else tool failure
        CR->>LLM: complete(messages + tool_results)
        LLM-->>CR: "Erro ao abrir Typora: ..."
    end

    CR-->>Kernel: RunResult output=Pronto
    Kernel-->>Caller: Pronto
```

## Fluxo de Execucao — Brief Mode (Loop)

No modo `brief`, o ChatRunner entra num **loop iterativo**: chama o LLM, executa tools, alimenta os resultados de volta ao LLM, e repete ate o LLM responder sem tool calls, ou o `LoopController` detectar um problema.

```mermaid
sequenceDiagram
    participant CR as ChatRunner
    participant LC as LoopController
    participant LLM
    participant TG as ToolGateway
    participant WM as WorkingMemory
    participant MC as MemoryConsolidator

    Note over CR: _run_loop()
    CR->>LLM: complete(messages)
    LLM-->>CR: text + tool_calls

    loop ate max_iterations ou LLM sem tool_calls
        CR->>TG: execute_tool_calls(calls)
        TG-->>CR: results

        CR->>LC: record(tool_id, params, success)
        CR->>LC: should_stop()

        alt stagnation (mesma tool+params 2x)
            LC-->>CR: true, stagnation
            CR->>LLM: complete(messages + injection_msg)
            Note over CR: Tenta corrigir com mensagem de injecao
        else circuit_breaker (3 falhas consecutivas)
            LC-->>CR: true, circuit_breaker
            Note over CR: Para o loop, retorna ultimo texto
        else on_after_tool_result hook
            CR->>CR: hook returns texto, break
        else continua
            CR->>CR: feed results back
            CR->>CR: compact_loop_messages()
            CR->>LLM: complete(messages + results)
            LLM-->>CR: text + tool_calls
        end
    end

    CR->>WM: update_message(response)
    CR->>MC: consolidate_if_needed(wm)
    CR-->>CR: return RunResult(output, loop_trace)
```

## LoopController — Guardiao do Loop

O `LoopController` protege contra tres cenarios degenerados:

```mermaid
stateDiagram-v2
    [*] --> Running: iteration 1
    Running --> Running: record(tool, params, success)

    Running --> Stagnation: mesma tool + mesmos params 2x seguidas
    Running --> CircuitBreaker: mesma tool falhou 3x seguidas
    Running --> MaxIterations: atingiu max_tool_iterations

    Stagnation --> InjectionAttempt: injeta mensagem de correcao
    InjectionAttempt --> Running: LLM tenta outra abordagem
    InjectionAttempt --> Stopped: falhou novamente

    CircuitBreaker --> Stopped
    MaxIterations --> Stopped

    Running --> EndTurn: LLM responde sem tool_calls
    EndTurn --> [*]
    Stopped --> [*]
```

## Long-Run Mode (Planner → Blocos → Evaluator)

No modo `long_run`, a execucao e dividida em **blocos planejados**. Um Planner LLM gera o plano, cada bloco roda como uma mini-sessao `brief`, e um Evaluator LLM avalia a qualidade de cada bloco. O `context_strategy` controla como o contexto e gerenciado entre blocos.

```mermaid
flowchart TB
    subgraph Planning
        A[User Task] --> B[Planner LLM]
        B --> C["LongRunPlan: blocks[]"]
    end

    subgraph Execution
        C --> D{Para cada bloco}
        D --> E[ChatRunner._run_loop<br/>com contexto do bloco]
        E --> F[BlockResult]
        F --> G[Evaluator LLM]
        G --> H{score >= threshold?}
        H -- Sim --> I[Proximo bloco]
        H -- Nao --> J[Retry com feedback]
        J --> E
        I --> D
    end

    subgraph CtxStrategy["Context Strategy"]
        K[compaction: compacta mensagens antigas]
        L[reset: limpa entre blocos]
        M[hybrid: compacta + reset seletivo]
    end

    subgraph Output
        D -- todos completos --> N[RunResult]
        N --> O["block_results[]"]
        N --> P["handoff_data"]
    end

    Execution -.-> CtxStrategy
```

## Scoring Automatico

Cada sessao recebe um score automatico baseado no `LoopTrace`:

```mermaid
flowchart LR
    subgraph Inputs
        SR[stop_reason]
        IC[iteration_count]
        FR[failure_rate]
    end

    subgraph Base Score
        SR --> |end_turn| S1[1.0]
        SR --> |stagnation| S2[0.2]
        SR --> |circuit_breaker| S3[0.1]
        SR --> |max_iterations| S4[0.0]
    end

    subgraph Penalties
        S1 --> P1["iteration_penalty<br/>(mais iteracoes = menor score)"]
        P1 --> P2["failure_penalty<br/>base *= 1 - failure_rate * 0.3"]
    end

    P2 --> FS[auto_score: 0.0-1.0]
    US[user_score] --> FINAL["final = auto*0.6 + user*0.4"]
    FS --> FINAL
```

## Notas

- O modo `auto` e resolvido pelo `ContextAssembler._resolve_auto_mode()` baseado em heuristicas do input do usuario (ex: "pesquise" → long_run, "abra" → instant).
- O `on_after_tool_result` hook (adicionado nesta versao) permite ao caller interceptar o resultado das tools e decidir se o loop deve parar — essencial para o SymTalk evitar a chamada LLM extra em fire-and-forget.
- O `MemoryConsolidator` roda em background thread apos cada resposta do ChatRunner, garantindo que sessoes longas nao estourem o contexto.
