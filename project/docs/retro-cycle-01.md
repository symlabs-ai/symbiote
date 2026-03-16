# Retro Note — Cycle 01 (Symbiote)

> Data: 2026-03-16
> Ciclo: cycle-01
> Status: MVP concluído

---

## O que funcionou

- **Paralelização de tasks independentes** — lançar 2-3 agents em paralelo por sprint acelerou muito. Tasks sem dependência mútua (ex: T-08 MemoryStore + T-09 KnowledgeService) rodaram simultâneamente sem conflitos.
- **TDD disciplinado** — RED→GREEN em cada task evitou regressões. 393 testes no total, cobertura consistente >95%.
- **Sprint Expert Gate** — review externo ao final de cada sprint identificou problemas reais (path traversal, domain exceptions, dependency inversion) que teriam sido bugs em produção.
- **Domain exceptions desde cedo** — `EntityNotFoundError`, `ValidationError`, `CapabilityError` criados na sprint-02 e usados consistentemente.
- **Ports como Protocols** — `StoragePort`, `LLMPort`, `MemoryPort`, `KnowledgePort` em `core/ports.py` mantiveram a arquitetura Clean/Hex limpa.

## O que não funcionou

- **CLI inicialmente desconectada do kernel** — sprint-07 criou CLI delegando para managers individuais, não para SymbioteKernel. Reescrita necessária para wiring correto dos value tracks.
- **Pre-commit hooks com ruff** — configs incorretas (path para ruff.toml, SIM105/SIM102/B904) causaram vários ciclos de commit. Deveria ter sido validado na sprint-01.
- **Line endings Windows** — smoke test falhou por `\r` em shell script. Fixado com `sed -i 's/\r$//'`.
- **ProcessEngine com cache in-memory** — mantém `_instances` dict que não invalida em multi-worker. Debt anotado.

## Métricas

| Métrica | Valor |
|---------|-------|
| Tasks concluídas | 24/24 (100%) |
| Testes totais | 393 |
| Cobertura | ~96% |
| Sprints | 8 |
| Expert Gates | 8 PASS (3 com BLOCK inicial corrigido) |
| Commits | 10 |

## Foco do próximo ciclo

Se houver cycle-02, priorizar:
- Docker container de referência
- Integração com LLM real (Anthropic/OpenAI) testada ponta-a-ponta
- `MessageRepository` port para isolar queries SQL do ReflectionEngine
- Semantic recall provider (keyword-based MVP)
- Interactive chat mode na CLI (loop input/output)

## Validação real

- **Comando executado**: `bash tests/smoke/smoke_cli.sh`
- **Input injetado**: 11 comandos CLI em sequência (create→list→session→chat→learn→teach→show→reflect→memory→export→close)
- **Output observado**: todos os comandos retornaram exit 0, output formatado com Rich
- **Freeze detectado**: nenhum
- **Status**: PASSOU
