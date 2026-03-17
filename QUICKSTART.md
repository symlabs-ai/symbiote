# Quickstart — Symbiote

## Instalação

```bash
git clone <repo-url> symbiote
cd symbiote
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Deployment Architectures

Symbiote supports two deployment models. Choose based on your needs:

### Embedded Library (recommended for single-product use)

The Symbiote kernel runs inside your application process. No extra services, ports, or infrastructure. Your app imports the kernel directly and calls it as Python functions.

```
Your App (FastAPI, Django, Flask...)
├── Your routes and business logic
├── SymbioteKernel (in-process)
│   └── SQLite: data/symbiote.db
└── LLM adapter (Anthropic, OpenAI, etc.)
```

**When to use:** Your product has one or a few Symbiotas dedicated to it. Simplest setup — zero network overhead, no extra deployment.

```python
# In your app's startup
from symbiote.core.kernel import SymbioteKernel
from symbiote.config.models import KernelConfig

kernel = SymbioteKernel(
    config=KernelConfig(db_path="data/symbiote.db"),
    llm=your_llm_adapter,
)
app.state.kernel = kernel

# In your routes — direct Python call, no HTTP
response = kernel.message(session.id, user_message, extra_context={...})
```

### HTTP API Service (for multi-product / shared infrastructure)

Symbiote runs as a standalone service with its own port. Multiple products communicate via REST API.

```
Product A ──→ POST /sessions/{id}/messages ──→ ┌──────────────┐
Product B ──→ GET  /symbiotes/{id}/tools   ──→ │  Symbiote    │
Product C ──→ POST /symbiotes              ──→ │  HTTP API    │
                                               │  port 8011   │
                                               │  SQLite/DB   │
                                               └──────────────┘
```

**When to use:** Multiple products share Symbiotas, or you need to scale/deploy the kernel independently from your app.

```bash
uvicorn symbiote.api.http:app --host 0.0.0.0 --port 8011
```

```python
# In your app — HTTP calls
import httpx
resp = httpx.post("http://symbiote:8011/sessions", json={...})
```

### Migration path

Start embedded. If you later need shared infrastructure, switch to HTTP — the API surface is identical to the Python library, so the change is mechanical (Python calls become HTTP calls).

---

## Uso via Python (biblioteca)

```python
from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel

# 1. Criar kernel
config = KernelConfig(db_path="my_project/.symbiote/symbiote.db")
llm = MockLLMAdapter(default_response="Hello! I remember everything.")
kernel = SymbioteKernel(config=config, llm=llm)

# 2. Criar simbióta
sym = kernel.create_symbiote(
    name="Atlas",
    role="assistant",
    persona={"tone": "friendly", "expertise": "python"},
)
print(f"Symbiote: {sym.name} ({sym.id})")

# 3. Abrir sessão
session = kernel.start_session(sym.id, goal="Help with Python project")
print(f"Session: {session.id}")

# 4. Conversar (Chat)
response = kernel.message(session.id, "How do I use dataclasses?")
print(f"Assistant: {response}")

# 5. Ensinar um fato (Learn)
entry = kernel.capabilities.learn(
    symbiote_id=sym.id,
    session_id=session.id,
    content="User prefers type hints in all code",
    fact_type="preference",
    importance=0.9,
)
print(f"Learned: {entry.content} (importance={entry.importance})")

# 6. Consultar (Teach)
explanation = kernel.capabilities.teach(
    symbiote_id=sym.id,
    session_id=session.id,
    query="type hints",
)
print(f"Teach:\n{explanation}")

# 7. Exibir dados (Show)
output = kernel.capabilities.show(
    symbiote_id=sym.id,
    session_id=session.id,
    query="type hints",
)
print(f"Show:\n{output}")

# 8. Refletir (Reflect)
result = kernel.capabilities.reflect(
    symbiote_id=sym.id,
    session_id=session.id,
)
print(f"Reflect: {result['message_count']} messages, summary: {result['summary'][:100]}")

# 9. Fechar sessão (roda reflexão + gera summary)
closed = kernel.close_session(session.id)
print(f"Session closed. Summary: {closed.summary}")

# 10. Reabrir — memória persiste entre sessões
session2 = kernel.start_session(sym.id, goal="Continue")
memories = kernel._memory.search("type hints")
print(f"Recovered {len(memories)} memories from previous session")

kernel.shutdown()
```

## Uso via CLI

```bash
# Criar simbióta
symbiote create --name "Atlas" --role "assistant" --persona-json '{"tone": "friendly"}'
# → Created symbiote: <UUID>

# Listar simbiótas
symbiote list

# Iniciar sessão
symbiote session start <SYMBIOTE_ID> --goal "Python help"
# → Started session: <UUID>

# Conversar (Value Track: Chat)
symbiote --llm mock chat <SESSION_ID> "How do I use dataclasses?"

# Ensinar um fato (Value Track: Learn)
symbiote learn <SESSION_ID> "User prefers type hints" --type preference --importance 0.9

# Consultar (Value Track: Teach)
symbiote teach <SESSION_ID> "type hints"

# Executar tarefa (Value Track: Work)
symbiote --llm mock work <SESSION_ID> "chat: explain decorators" --intent chat

# Exibir dados (Value Track: Show)
symbiote show <SESSION_ID> "type hints"

# Refletir (Value Track: Reflect)
symbiote reflect <SESSION_ID>

# Buscar memórias
symbiote memory search "type hints" --scope global --limit 5

# Exportar sessão como Markdown
symbiote export session <SESSION_ID>

# Fechar sessão
symbiote session close <SESSION_ID>
```

## Uso via API HTTP

```bash
# Iniciar servidor
uvicorn symbiote.api.http:app --host 0.0.0.0 --port 8000

# Criar simbióta
curl -X POST http://localhost:8000/symbiotes \
  -H "Content-Type: application/json" \
  -d '{"name": "Atlas", "role": "assistant"}'

# Criar sessão
curl -X POST http://localhost:8000/sessions \
  -H "Content-Type: application/json" \
  -d '{"symbiote_id": "<UUID>", "goal": "Python help"}'

# Enviar mensagem
curl -X POST http://localhost:8000/sessions/<SESSION_ID>/messages \
  -H "Content-Type: application/json" \
  -d '{"role": "user", "content": "How do I use dataclasses?"}'

# Fechar sessão
curl -X POST http://localhost:8000/sessions/<SESSION_ID>/close

# Buscar memórias
curl "http://localhost:8000/memory/search?query=type+hints&limit=5"
```

## Configuração de LLM

```bash
# Mock (default — respostas fixas, para testes)
symbiote --llm mock chat <SESSION_ID> "hello"

# Anthropic (requer ANTHROPIC_API_KEY)
export ANTHROPIC_API_KEY="sk-..."
symbiote --llm anthropic chat <SESSION_ID> "hello"

# OpenAI (requer OPENAI_API_KEY)
export OPENAI_API_KEY="sk-..."
symbiote --llm openai chat <SESSION_ID> "hello"

# Via env var (persistente)
export SYMBIOTE_LLM_PROVIDER=anthropic
symbiote chat <SESSION_ID> "hello"
```

## Uso como biblioteca embutida

```python
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel
from symbiote.adapters.llm.forge import ForgeLLMAdapter

# Em outro sistema Python
kernel = SymbioteKernel(
    config=KernelConfig(db_path="/app/data/symbiote.db"),
    llm=ForgeLLMAdapter(provider="anthropic"),
)

# Criar simbióta dedicado ao seu sistema
sym = kernel.create_symbiote(
    name="SupportBot",
    role="customer_support",
    persona={
        "tone": "professional",
        "constraints": ["never share internal data", "always be helpful"],
    },
)

# Usar em endpoints do seu app
def handle_user_message(user_id: str, message: str) -> str:
    session = kernel.start_session(sym.id, goal=f"Support for {user_id}")
    response = kernel.message(session.id, message)
    kernel.close_session(session.id)
    return response
```
