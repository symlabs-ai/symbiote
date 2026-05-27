# Symbiote

> Kernel for creating and managing persistent cognitive entities.

Symbiote is the runtime infrastructure for AI-powered assistants ("Symbiotas") that maintain persistent identity, long-term memory, and can execute actions in their environment — all through a clean, embeddable Python kernel.

## Features

### Core
- **Identity & Persona** — each Symbiota has a name, role, and configurable persona with audit trail
- **4-layer Memory Stack** — working memory, session summaries, long-term relational, semantic recall
- **Memory Categories** — automatic classification into ephemeral, declarative, procedural, meta
- **Context Assembly** — token-budget-aware pipeline that ranks and assembles context for LLM calls
- **6 Capabilities** — Learn, Teach, Chat, Work, Show, Reflect as explicit operations
- **3 Interfaces** — Python library, CLI (Typer + Rich), HTTP API (FastAPI)

### Tools & Execution
- **Tool System** — register tools with JSON Schema descriptors, execute through deny-by-default PolicyGate with full audit log
- **HTTP Tools** — declarative HTTP tool definitions (method + URL template) — no code required
- **Tool Loop** — agentic multi-step execution with automatic context compaction
- **Tool Call Parser** — LLM responses with `tool_call` blocks are automatically parsed and executed
- **Lifecycle Hooks** — composable pre/post hooks for tool execution and chat turns (audit, metrics, rate-limiting)

### Integration
- **Extra Context Injection** — host applications can inject page context, user state, etc. into the LLM prompt
- **Session External Keys** — map external identifiers (e.g. `user_id:page_url`) to sessions for easy integration
- **Session Recall Port** — pluggable search interface for host-provided transcript search
- **Message Bus** — async inbound/outbound queues with retry, backoff, and delta streaming
- **Prompt Caching** — optional Anthropic prompt cache optimization via `EnvironmentConfig`

### Reliability
- **Per-session Locks** — concurrent requests on the same session are serialized; different sessions run in parallel
- **SSRF Protection** — URL validation blocks private/internal IPs; `allow_internal` hardened against config injection
- **Process Engine** — declarative step-by-step workflows
- **Reflection Engine** — automatic fact extraction with semantic type classification

### Web & Browser (v0.6.0+)
- **Web Search** — `web_search` tool backed by Brave Search via SymGateway proxy (centralized billing, no extra credentials)
- **Web Extract** — `web_extract` with platform-aware extraction (YouTube transcripts, Reddit posts, Twitter, Instagram, generic markdown via trafilatura). Configurable provider chain: forge_scraper primary → Firecrawl fallback
- **Web Crawl** — `web_crawl` for instruction-driven domain crawls via Firecrawl
- **Browser Automation** — `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_fill`, `browser_screenshot`, `browser_wait_for`, `browser_close` via Playwright (Chromium local)
- **Website Policy** — domain blocklist/allowlist with wildcard matching (`*.ads.com`) and TTL cache
- **Opt-in subpackage** — `symbiote.browser` ships in the package but stays inert until `register(kernel, ...)` is called. Zero impact on existing clients.

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
│  ├─ CompositeHook      ├─ SessionLock            │
│  ├─ MessageBus         ├─ ExportService          │
│  └─ WorkspaceManager   └─ SessionRecallPort*     │
├──────────────────────────────────────────────────┤
│  Adapters: SQLite │ LLM (forge_llm)             │
└──────────────────────────────────────────────────┘
  * SessionRecallPort: host provides implementation
```

## Host Integration Guide

The kernel provides ports and hooks for hosts to extend behavior without modifying core code.

### Session Recall (search past conversations)

```python
class MySessionRecall:
    """Host-provided search over past sessions. Use FTS5, embeddings, etc."""

    def search_messages(self, query, symbiote_id=None, session_id=None, limit=10):
        # Your search logic here — FTS5, Elasticsearch, embedding similarity, etc.
        return [{"session_id": "...", "role": "user", "content": "...", "timestamp": "..."}]

    def search_sessions(self, query, symbiote_id=None, limit=5):
        return [{"session_id": "...", "goal": "...", "summary": "..."}]

kernel.set_session_recall(MySessionRecall())
```

### Lifecycle Hooks (audit, metrics, approval gates)

```python
from symbiote.core.hooks import BaseHook

class AuditHook(BaseHook):
    async def before_tool(self, tool_id, params):
        log.info(f"Tool call: {tool_id}")

    async def after_tool(self, tool_id, params, result):
        log.info(f"Tool result: {tool_id} -> {result}")

kernel.hooks.add(AuditHook())
```

### Prompt Caching (Anthropic ~90% token saving)

```python
kernel.environment.configure(symbiote_id=bot.id, prompt_caching=True)
```

### Message Bus (channel integration with streaming)

```python
from symbiote.bus.message_bus import MessageBus
from symbiote.bus.events import StreamDelta

bus = MessageBus()

# Consume streaming deltas for real-time UX
async def stream_to_websocket(ws):
    while True:
        delta = await bus.receive_delta(timeout=30.0)
        if delta is None:
            break
        await ws.send(delta.delta)
        if delta.is_final:
            break
```

### Memory Categories

Memories are auto-classified into categories for policy and retrieval:

| Category | Types | Purpose |
|----------|-------|---------|
| `ephemeral` | working | Short-lived, auto-expires |
| `declarative` | preference, constraint, factual, decision, relational | Facts about the world |
| `procedural` | procedural | How-to knowledge, workflows |
| `meta` | session_summary, reflection, semantic_note | About other memories |

```python
# Query by category
procedural = store.get_by_category(symbiote_id, "procedural")
```

## Web & Browser tools (v0.6.0+)

`symbiote.browser` adds web search, content extraction, and full browser automation as opt-in tools registered on the `ToolGateway`. Activation is a single line:

```python
from symbiote.core.kernel import SymbioteKernel
from symbiote.browser import register

kernel = SymbioteKernel(config, llm)

register(
    kernel,
    search_backend="brave",                            # web_search via SymGateway
    extract_backend=["forge_scraper", "firecrawl"],    # chain with fallback
    browser_backend="chromium",                        # browser_* via Playwright
    policy={"blocklist": ["*.ads.com", "doubleclick.net"]},
)
```

The Symbiota can now use 10 new tools: `web_search`, `web_extract`, `web_crawl`, `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_fill`, `browser_screenshot`, `browser_wait_for`, `browser_close`.

### Optional extras

| Extra | Includes | When to install |
|---|---|---|
| `pip install "symbiote[extract]"` | forge_scraper + trafilatura + youtube-transcript-api | Web content extraction (free for public URLs) |
| `pip install "symbiote[browser]"` | Playwright | Browser automation. Run `playwright install chromium` after install (~170MB, one-time) |
| `pip install "symbiote[stealth]"` | playwright-stealth | Reserved for anti-fingerprinting (not yet wired) |

Without these extras the package stays slim — `import symbiote` adds no extra dependencies, and tools only appear in the kernel after `register()` is called.

### Cost

| Tool | Provider | Cost |
|---|---|---|
| `web_search` | Brave via SymGateway | $0.003/query (billed via SymGateway) |
| `web_extract` (public URLs) | forge_scraper local | $0 |
| `web_extract` (Cloudflare/etc) | Firecrawl via SymGateway | per Firecrawl pricing |
| `web_crawl` | Firecrawl via SymGateway | per Firecrawl pricing × pages |
| `browser_*` | Chromium local | $0 (your CPU/RAM) |

### Visual demo

```bash
.venv/bin/python scripts/demo_browser.py --headed --slow-mo 1500
```

Opens a real Chromium window, navigates, snapshots, clicks, closes. Useful for validation and debugging.

## Documentation

- **[QUICKSTART.md](QUICKSTART.md)** — Full usage guide (Python, CLI, HTTP API, embedded)
- **[docs/releases/v0.6.0.md](docs/releases/v0.6.0.md)** — Release report for clients: quickstart, examples, cost cheat-sheet, migration
- **[docs/integrations/symbiote-browser-quickstart.md](docs/integrations/symbiote-browser-quickstart.md)** — Detailed `symbiote.browser` user guide (setup, troubleshooting, FAQ)
- **[docs/integrations/symbiote-browser.md](docs/integrations/symbiote-browser.md)** — Architectural plan and decisions for the browser subpackage

## License

Dual-licensed:

- **[AGPL-3.0](LICENSE)** — free for open-source use
- **[Commercial](LICENSE-COMMERCIAL.md)** — available for proprietary applications

Copyright 2026 [Symlabs](https://symlabs.ai).
