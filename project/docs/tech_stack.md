# Tech Stack — Symbiote

> Data: 2026-03-16
> Status: draft

---

## Linguagem

| Item | Escolha | Justificativa |
|------|---------|---------------|
| Linguagem | Python 3.12+ | Ecossistema de IA, tipagem forte com type hints, async nativo, público-alvo são devs Python |

---

## Core

| Camada | Escolha | Papel |
|--------|---------|-------|
| Validação/Config | **Pydantic v2** | Modelos de domínio, config, serialização, validação de entrada |
| Persistência | **SQLite** (stdlib `sqlite3`) | Fonte de verdade para identidade, sessões, memória, decisões. Zero infra externa |
| Migrações | **Manual (versioned SQL)** | Scripts SQL versionados em `adapters/storage/migrations/`. Alembic é overkill para SQLite no MVP |
| Filesystem | **pathlib** (stdlib) | Workspaces, artefatos, exports. Sem abstração extra |
| Serialização | **PyYAML** | Config files, process definitions |

---

## Interfaces

| Interface | Escolha | Papel |
|-----------|---------|-------|
| CLI | **Typer** | CLI local para interação, debug e automação |
| HTTP API | **FastAPI** + **Uvicorn** | Serviço HTTP para modo runtime persistente |
| Biblioteca | **Python package** (`pip install`) | Uso embutido via `from symbiote.core.kernel import SymbioteKernel` |

---

## LLM

| Item | Escolha | Papel |
|------|---------|-------|
| Abstração | **LLMAdapter** (interface própria) | Contrato fino: `complete(messages, config) -> response`. Sem framework |
| Provider default | **ForgeLLM** (`forge-llm`) | Client Symlabs para Anthropic/OpenAI/OpenRouter. Já instalado no projeto |
| Fallback testes | **MockLLMAdapter** | Respostas determinísticas para testes sem API key |

---

## Framework Base — ForgeBase

| Item | Escolha | Papel |
|------|---------|-------|
| Arquitetura | **ForgeBase** (`forge_base`) | Framework base Clean/Hex. Define a estrutura de domínio, ports, adapters, use cases e entrypoints |
| Use Cases | **`forge_base.pulse.UseCaseRunner`** | Toda execução de use case passa pelo runner — nunca chamar `.execute()` direto nos entrypoints |
| Observabilidade | **ForgeBase Pulse** | Métricas por value track: count, duration, success, error. Eventos: start, finish, error |
| Domínio | **Puro** | `core/`, `memory/`, `knowledge/` — sem I/O, sem imports de adapters. Ports via Protocol/ABC |
| Adapters | **`adapters/`** | Implementações concretas dos ports (SQLite, LLM, filesystem, export) |
| Entrypoints | **CLI + HTTP** | Apenas delegam para UseCases. Não contêm lógica de domínio |

---

## Observabilidade

| Item | Escolha | Papel |
|------|---------|-------|
| Logging | **structlog** | Logs estruturados JSON com session_id, process_id, symbiote_id |
| UX terminal | **Rich** (via Typer) | Formatação de output na CLI (tables, markdown, progress) |

---

## Testes

| Item | Escolha | Papel |
|------|---------|-------|
| Framework | **pytest** | Testes unitários e de integração |
| Cobertura | **pytest-cov** | Mínimo 85%, desejável 90% |
| Linting | **Ruff** | Linter + formatter, rápido, configurável |
| Type checking | **mypy** (strict) | Validação estática de tipos |
| Pre-commit | **pre-commit** | Hooks para ruff + mypy antes de cada commit |

---

## Arquitetura

| Princípio | Implementação |
|-----------|---------------|
| Clean/Hex via ForgeBase | Domínio puro em `core/`, `memory/`, `knowledge/`. Adapters em `adapters/`. Ports via Protocol/ABC. UseCases via UseCaseRunner |
| Local-first | SQLite + filesystem. Sem rede obrigatória exceto para LLM |
| Extensível | LLMAdapter, StorageAdapter, SemanticRecallProvider como interfaces plugáveis (ports ForgeBase) |
| Sem framework de agentes | Kernel próprio sobre ForgeBase. Sem LangChain, CrewAI, AutoGen |

---

## Dependências do MVP

```
# Core
pydantic>=2.0,<3.0
pyyaml>=6.0
structlog>=24.0,<25.0

# Interfaces
typer>=0.12
rich>=13.0
fastapi>=0.115
uvicorn>=0.30

# HTTP client (para LLM adapters)
httpx>=0.27,<0.28

# ForgeBase (observabilidade)
forge-base  # git+https://github.com/symlabs-ai/forgebase.git

# ForgeLLM (LLM provider, opcional)
forge-llm   # git+https://github.com/symlabs-ai/forgellmclient.git
```

---

## Alternativas Descartadas

| Alternativa | Motivo da rejeição |
|-------------|-------------------|
| **SQLAlchemy + Alembic** | Overhead de ORM para SQLite simples. O MVP tem ~10 tabelas com queries diretas. Cru é mais debugável e portável |
| **Postgres** | Exige infra externa. Viola local-first. Pode ser adapter futuro |
| **LangChain / CrewAI** | Dependência estrutural pesada. Symbiote é um kernel, não um wrapper. Decisão arquitetural #6 |
| **ChromaDB / Pinecone** | Dependência obrigatória de vector DB viola princípio local-first. SemanticRecallProvider é interface para plug futuro |
| **Click** (CLI) | Typer é baseado em Click mas com type hints nativos e menos boilerplate |
| **Django** (HTTP) | Pesado para API stateless. FastAPI é mais leve e tem async nativo |
| **aiosqlite** | Async não é necessário no MVP para SQLite local. stdlib sqlite3 é suficiente |

---

## Dúvidas para o Stakeholder

Nenhuma — stack alinhada com as decisões fixadas no PRD (seção 7.2).
