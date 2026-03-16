# Symbiote

> Kernel for creating and managing persistent cognitive entities.

Symbiote is the runtime infrastructure for AI-powered assistants ("Symbiotas") that maintain persistent identity, long-term memory, and can execute actions in their environment — all through a clean, embeddable Python kernel.

## Features

- **Identity & Persona** — each Symbiota has a name, role, and configurable persona with audit trail
- **4-layer Memory Stack** — working memory, session summaries, long-term relational, semantic recall
- **Context Assembly** — token-budget-aware pipeline that ranks and assembles context for LLM calls
- **Tool System** — register tools with JSON Schema descriptors, execute through deny-by-default PolicyGate with full audit log
- **HTTP Tools** — declarative HTTP tool definitions (method + URL template) — no code required
- **Tool Call Parser** — LLM responses with `tool_call` blocks are automatically parsed and executed
- **Extra Context Injection** — host applications can inject page context, user state, etc. into the LLM prompt
- **Session External Keys** — map external identifiers (e.g. `user_id:page_url`) to sessions for easy integration
- **6 Capabilities** — Learn, Teach, Chat, Work, Show, Reflect as explicit operations
- **3 Interfaces** — Python library, CLI (Typer + Rich), HTTP API (FastAPI)
- **Process Engine** — declarative step-by-step workflows
- **Reflection Engine** — automatic fact extraction and session summarization

## Quick Start

```bash
pip install -e ".[dev]"
```

### Python (library)

```python
from symbiote.core.kernel import SymbioteKernel
from symbiote.config.models import KernelConfig
from symbiote.adapters.llm.base import MockLLMAdapter

kernel = SymbioteKernel(
    config=KernelConfig(db_path="data/symbiote.db"),
    llm=MockLLMAdapter(default_response="Hello!"),
)

# Create a Symbiota
sym = kernel.create_symbiote(name="Atlas", role="assistant")

# Start a session and chat
session = kernel.start_session(sym.id, goal="Python help")
response = kernel.message(session.id, "How do I use dataclasses?")

# Close session (runs reflection, generates summary)
kernel.close_session(session.id)
```

### CLI

```bash
symbiote create --name "Atlas" --role "assistant"
symbiote session start <SYMBIOTE_ID> --goal "Python help"
symbiote --llm mock chat <SESSION_ID> "How do I use dataclasses?"
symbiote session close <SESSION_ID>
```

### HTTP API

```bash
uvicorn symbiote.api.http:app --host 0.0.0.0 --port 8000

curl -X POST http://localhost:8000/symbiotes \
  -H "Content-Type: application/json" \
  -d '{"name": "Atlas", "role": "assistant"}'
```

### Embedded (in your app)

```python
from symbiote.core.kernel import SymbioteKernel
from symbiote.environment.descriptors import ToolDescriptor, HttpToolConfig

# Register domain-specific tools
kernel.tool_gateway.register_http_tool(
    ToolDescriptor(
        tool_id="search",
        name="Search",
        description="Search articles",
        parameters={"type": "object", "properties": {"q": {"type": "string"}}},
    ),
    HttpToolConfig(method="GET", url_template="http://localhost:8000/api/search?q={q}"),
)

# Use external keys for session mapping
session = kernel.get_or_create_session(
    symbiote_id=bot.id,
    external_key=f"{user_id}:{page_url}",
)

# Inject page context
response = kernel.message(
    session.id,
    user_message,
    extra_context={"page_url": page_url, "page_content": visible_text},
)
```

See [QUICKSTART.md](QUICKSTART.md) for the full guide.

## Architecture

```
┌──────────────────────────────────────────────────┐
│  Interfaces: Python Library │ CLI │ HTTP API     │
├──────────────────────────────────────────────────┤
│  SymbioteKernel (orchestrator)                   │
│  ├─ IdentityManager    ├─ SessionManager         │
│  ├─ MemoryStore        ├─ KnowledgeService       │
│  ├─ ContextAssembler   ├─ RunnerRegistry         │
│  ├─ ToolGateway        ├─ PolicyGate             │
│  ├─ ReflectionEngine   ├─ ProcessEngine          │
│  └─ ExportService      └─ WorkspaceManager       │
├──────────────────────────────────────────────────┤
│  Adapters: SQLite │ LLM (Mock/Anthropic/OpenAI)  │
└──────────────────────────────────────────────────┘
```

## License

Dual-licensed:

- **[AGPL-3.0](LICENSE)** — free for open-source use
- **[Commercial](LICENSE-COMMERCIAL.md)** — available for proprietary applications

Copyright 2026 [Symlabs](https://symlabs.ai).
