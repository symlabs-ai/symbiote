# Host Integration Guide

This guide covers how to integrate Symbiote into your application (the "host"). Symbiote is an embeddable kernel -- your app owns the LLM, the tools, the sessions, and the user-facing interface. The kernel handles identity, memory, context assembly, tool execution, and self-improvement.

---

## Kernel Initialization

```python
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel
from symbiote.adapters.llm.forge import ForgeLLMAdapter

# 1. Create LLM adapter (host provides this)
llm = ForgeLLMAdapter(provider="anthropic", model="claude-sonnet-4-20250514")

# 2. Create kernel
kernel = SymbioteKernel(
    config=KernelConfig(
        db_path="data/symbiote.db",
        context_budget=8000,
    ),
    llm=llm,
)

# 3. Create or find symbiote
sym = kernel.find_symbiote_by_name("my-agent")
if sym is None:
    sym = kernel.create_symbiote(
        name="my-agent",
        role="assistant",
        persona={
            "tone": "professional",
            "expertise": "domain-specific",
            "constraints": ["never share internal data"],
        },
    )
```

---

## Tool Registration

### HTTP Tools

Wrap REST endpoints as tools the LLM can call:

```python
from symbiote.environment.descriptors import HttpToolConfig, ToolDescriptor

kernel.tool_gateway.register(
    descriptor=ToolDescriptor(
        tool_id="search_articles",
        name="Search Articles",
        description="Search published articles by keyword.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
        tags=["content"],
        risk_level="low",
    ),
    http_config=HttpToolConfig(
        method="GET",
        url_template="http://127.0.0.1:8000/api/articles?q={query}&limit={limit}",
        allow_internal=True,  # Same-host service (set programmatically only)
        header_factory=lambda: {"Authorization": f"Bearer {get_current_token()}"},
        optional_params=["limit"],
    ),
)

# Authorize the tool for the symbiote
kernel.environment.configure(symbiote_id=sym.id, tools=["search_articles"])
```

### MCP Tools

Bridge Model Context Protocol tools from forge-llm:

```python
from forge_llm.application.tools import McpToolset

async with McpToolset.from_http("http://localhost:9000/mcp") as registry:
    tool_ids = kernel.load_mcp_tools(registry, symbiote_id=sym.id)
    # tool_ids are automatically authorized
```

### Custom Handlers

Register arbitrary Python callables as tools:

```python
def my_custom_tool(params: dict) -> str:
    return f"Result for {params['query']}"

kernel.tool_gateway.register(
    descriptor=ToolDescriptor(
        tool_id="custom_search",
        name="Custom Search",
        description="Custom search implementation.",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    ),
    handler=my_custom_tool,
)
kernel.environment.configure(symbiote_id=sym.id, tools=["custom_search"])
```

### Discovered Tools

Automatically discover and load tools from a repository:

```python
# After running: symbiote discover /path/to/repo
# And approving tools via the dashboard or API
tool_ids = kernel.load_discovered_tools(sym.id, base_url="http://127.0.0.1:8000")
```

#### Risk levels from discovery

Discovery assigns a `risk_level` to every discovered tool and propagates it all
the way through to the loaded `ToolDescriptor`, so your approval callback can
distinguish a low-risk read from a destructive write **without** maintaining a
parallel risk map in the host.

Two sources, in precedence order:

1. **Explicit `x-risk-level` OpenAPI extension** on the operation
   (`low` | `medium` | `high`). This is the recommended way for a host to
   declare intent:

   ```python
   @app.delete("/api/goals/{goal_id}", openapi_extra={"x-risk-level": "high"})
   def delete_goal(goal_id: int): ...

   @app.post("/api/habits/log", openapi_extra={"x-risk-level": "low"})
   def log_habit(...): ...   # a POST that is cheap/reversible → low
   ```

2. **HTTP-method heuristic** (fallback when `x-risk-level` is absent or invalid):
   `GET`/`HEAD`/`OPTIONS` → `low`, `POST`/`PUT`/`PATCH` → `medium`,
   `DELETE` → `high`.

The same precedence applies to live-URL discovery (`discover(url=...)`) and
file-based OpenAPI specs. An unknown `x-risk-level` value is ignored and the
heuristic is used. `risk_level` is also exposed on the discovered-tools REST/HTTP
responses.

---

## Session Management

### External Keys

Use external keys to tie sessions to your application's identifiers (user ID, page URL, conversation ID). The `get_or_create_session` method is idempotent.

```python
# Same external_key always returns the same session
session = kernel.get_or_create_session(
    symbiote_id=sym.id,
    external_key=f"{user_id}:{page_url}",
    goal=f"Help user with {page_title}",
)
```

### Session Lifecycle

```python
# Start
session = kernel.start_session(sym.id, goal="Help with X")

# Send messages
response = kernel.message(session.id, user_message)

# Close (triggers reflection + scoring + failure memory)
closed = kernel.close_session(session.id)
# closed.summary contains the auto-generated session summary
```

---

## Context Injection

The `extra_context` parameter lets the host inject application-specific context into every message. This context is included in the assembled context but NOT stored in memory.

```python
response = kernel.message(
    session.id,
    user_message,
    extra_context={
        "current_url": "https://example.com/articles/123",
        "page_title": "Introduction to Python",
        "user_role": "editor",
        "recent_actions": ["published article", "edited comment"],
    },
)
```

The extra_context is available in `AssembledContext.extra_context` and included in the ChatRunner's system prompt.

### Injecting context into delegated (spawned) sub-sessions

The `spawn` tool (`SubagentManager`) also accepts `extra_context`, forwarded to
the delegated Symbiota's `kernel.message` and rendered in its own `## Context`
block. This lets a host pass deterministic, code-derived facts (OS, paths,
platform hints) to a sub-agent through the same per-turn channel — instead of
baking them into the sub-agent's persona.

```python
mgr.spawn({
    "target_symbiote": "install_agent",
    "task": "Install ripgrep",
    "extra_context": {"host_environment": "OS=Linux; pkg=apt"},
})
```

`extra_context` is intentionally **not** part of `SPAWN_DESCRIPTOR.parameters`,
so the calling LLM never authors it — it is a host-injection channel. The host
wires it in (e.g. by wrapping the spawn handler) based on the target. Omitting
it preserves the legacy behaviour (`None`).

---

## Streaming

### Token Streaming (async)

```python
async def handle_message(session_id: str, content: str):
    async def on_token(token: str):
        await websocket.send(token)

    response = await kernel.message_async(
        session_id,
        content,
        on_token=on_token,
    )
    return response
```

### Mid-Loop Progress Callbacks

For monitoring tool loop progress, configure the ChatRunner with callbacks:

```python
from symbiote.runners.chat import ChatRunner

def on_progress(event: str, iteration: int, total: int):
    # event: "tool_start", "tool_end", "llm_start", "llm_end"
    print(f"[{iteration}/{total}] {event}")

def on_stream(text: str, iteration: int):
    # Called with partial text during each LLM call
    print(f"[iter {iteration}] {text[:50]}...")

runner = ChatRunner(
    llm=llm,
    tool_gateway=kernel.tool_gateway,
    on_progress=on_progress,
    on_stream=on_stream,
)
```

---

## Feedback Reporting

Report user feedback to improve the harness evolution system:

```python
# After a thumbs-up
kernel.report_feedback(session.id, score=1.0, source="thumbs_up")

# After a thumbs-down
kernel.report_feedback(session.id, score=0.0, source="thumbs_down")

# After task completion (partial success)
kernel.report_feedback(session.id, score=0.7, source="task_completion")
```

The final score combines auto-score (60%) with user feedback (40%).

---

## Human-in-the-Loop

Tools can declare a `risk_level` (`"low"`, `"medium"`, `"high"`). The host provides an approval callback:

```python
from symbiote.runners.chat import ChatRunner

def approval_gate(tool_id: str, params: dict, risk: str) -> bool:
    if risk == "high":
        # Ask user for confirmation
        return ask_user(f"Allow {tool_id} with {params}?")
    if risk == "medium":
        # Log but allow
        logger.info(f"Medium-risk tool call: {tool_id}")
        return True
    return True  # Low risk: auto-approve

runner = ChatRunner(
    llm=llm,
    tool_gateway=kernel.tool_gateway,
    on_before_tool_call=approval_gate,
)
```

Set risk levels when registering tools:

```python
ToolDescriptor(
    tool_id="delete_article",
    name="Delete Article",
    description="Permanently delete an article.",
    risk_level="high",
    # ...
)
```

---

## Tool Mode Selection

Configure how the agent loop handles tool calls:

```python
# instant: Single-shot, 0-1 tool calls, fast-path
kernel.environment.configure(symbiote_id=sym.id, tool_mode="instant")

# brief: Multi-step task loop, 3-10 iterations (default)
kernel.environment.configure(symbiote_id=sym.id, tool_mode="brief")

# long_run: Project-scale with Planner/Generator/Evaluator
kernel.environment.configure(
    symbiote_id=sym.id,
    tool_mode="long_run",
    planner_prompt="Expand this into a detailed project plan...",
    evaluator_prompt="Evaluate strictly against criteria...",
    evaluator_criteria=[
        {"name": "completeness", "weight": 1.0, "threshold": 0.7,
         "description": "All requested features implemented"},
        {"name": "quality", "weight": 0.8, "threshold": 0.6,
         "description": "Code quality and design"},
    ],
    context_strategy="hybrid",  # compaction within blocks, reset between
    max_blocks=15,
)

# continuous: Always-on agent (placeholder, not yet implemented)
kernel.environment.configure(symbiote_id=sym.id, tool_mode="continuous")
```

---

## Context Mode

Choose how memory and knowledge reach the LLM:

```python
# packed (default): Memories and knowledge injected upfront in the system prompt
kernel.environment.configure(symbiote_id=sym.id, context_mode="packed")

# on_demand: Memories/knowledge available as tools (search_memories, search_knowledge)
# Reduces prompt size, good for symbiotes with large memory stores
kernel.environment.configure(symbiote_id=sym.id, context_mode="on_demand")
```

---

## Timeout Configuration

```python
kernel.environment.configure(
    symbiote_id=sym.id,
    tool_call_timeout=15.0,  # Per-tool timeout (seconds), default 30
    loop_timeout=120.0,      # Total loop timeout (seconds), default 300
)
```

---

## Evolver LLM Injection

The harness evolver can use a separate LLM from the main one. This avoids blind spots where the proposer model has the same weaknesses as the model being optimized for.

```python
# Use a different/cheaper model for evolution proposals
evolver_llm = ForgeLLMAdapter(provider="openai", model="gpt-4o-mini")
kernel.set_evolver_llm(evolver_llm)

# If not set, the kernel's main LLM is used as fallback
```

---

## Running the Parameter Tuner

Run periodically (e.g., weekly) to auto-calibrate parameters:

```python
from symbiote.harness.tuner import ParameterTuner

tuner = ParameterTuner(kernel._storage)

# Analyze last 7 days of session data
result = tuner.analyze(symbiote_id=sym.id, days=7)

print(f"Sessions analyzed: {result.session_count}")
print(f"Tier: {result.tier}")
print(f"Adjustments: {result.adjustments}")

# Apply changes
if result.adjustments:
    tuner.apply(result, kernel.environment)
```

See [HARNESS_EVOLUTION.md](HARNESS_EVOLUTION.md) for details on tier behavior and safety caps.

---

## Lifecycle Hooks

Register hooks for observability, metrics, or custom logic:

```python
from symbiote.core.hooks import BaseHook

class MetricsHook(BaseHook):
    async def before_tool(self, tool_id, params):
        metrics.start_timer(f"tool.{tool_id}")

    async def after_tool(self, tool_id, params, result):
        metrics.stop_timer(f"tool.{tool_id}")
        metrics.increment(f"tool.{tool_id}.calls")

    async def before_turn(self, messages):
        metrics.increment("llm.turns")

    async def after_turn(self, messages, response):
        metrics.record("llm.response_length", len(response))

kernel.hooks.add(MetricsHook())
```

---

## Session Recall (Host-Provided)

The kernel defines a `SessionRecallPort` protocol for searching past session transcripts. The host decides the implementation (FTS5, Elasticsearch, embeddings, etc.).

```python
from symbiote.core.ports import SessionRecallPort

class MySessionRecall:
    def search_messages(self, query, symbiote_id=None, session_id=None, limit=10):
        # Your FTS5 / embedding search implementation
        return [{"session_id": "...", "role": "assistant", "content": "...", "timestamp": "..."}]

    def search_sessions(self, query, symbiote_id=None, limit=5):
        return [{"session_id": "...", "goal": "...", "summary": "...", "started_at": "..."}]

kernel.set_session_recall(MySessionRecall())
```

---

## Complete Integration Example

```python
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel
from symbiote.adapters.llm.forge import ForgeLLMAdapter
from symbiote.environment.descriptors import HttpToolConfig, ToolDescriptor

# 1. Initialize
llm = ForgeLLMAdapter(provider="anthropic")
kernel = SymbioteKernel(
    config=KernelConfig(db_path="data/symbiote.db", context_budget=8000),
    llm=llm,
)

# 2. Create/find symbiote
sym = kernel.find_symbiote_by_name("clark") or kernel.create_symbiote(
    name="clark", role="journalist_assistant",
    persona={"expertise": "news", "tone": "concise"},
)

# 3. Register tools
kernel.tool_gateway.register(
    descriptor=ToolDescriptor(
        tool_id="search_articles", name="Search Articles",
        description="Search published articles.",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        tags=["content"], risk_level="low",
    ),
    http_config=HttpToolConfig(
        method="GET",
        url_template="http://127.0.0.1:8000/api/articles?q={query}",
        allow_internal=True,
    ),
)
kernel.environment.configure(symbiote_id=sym.id, tools=["search_articles"])

# 4. Configure harness
kernel.environment.configure(
    symbiote_id=sym.id,
    tool_mode="brief",
    max_tool_iterations=15,
    context_mode="packed",
    memory_share=0.35,
    knowledge_share=0.30,
)

# 5. Handle requests
async def handle_chat(user_id: str, message: str, page_url: str):
    session = kernel.get_or_create_session(
        symbiote_id=sym.id,
        external_key=f"{user_id}:{page_url}",
    )

    response = await kernel.message_async(
        session.id,
        message,
        extra_context={"url": page_url},
        on_token=lambda t: send_sse(t),
    )
    return response

# 6. Report feedback
def handle_feedback(session_id: str, thumbs_up: bool):
    kernel.report_feedback(session_id, score=1.0 if thumbs_up else 0.0)

# 7. Periodic tuning (cron job)
def weekly_tuning():
    from symbiote.harness.tuner import ParameterTuner
    from symbiote.harness.evolver import EVOLVABLE_COMPONENTS

    tuner = ParameterTuner(kernel._storage)
    result = tuner.analyze(sym.id, days=7)
    if result.adjustments:
        tuner.apply(result, kernel.environment)

    for component in EVOLVABLE_COMPONENTS:
        kernel.evolve_harness(sym.id, component, default_text="...")
        kernel.check_harness_rollback(sym.id, component)

# 8. Shutdown
kernel.shutdown()
```

---

## Inspecting & Administering an Embedded Symbiote

When the kernel runs **embedded** in your host app (e.g. SymTalk drops a
`SymbioteKernel` into its own process), there is no Symbiote server — but there
is no separate datastore either. **All state lives in a single SQLite file**:
symbiotes, sessions, messages, memory, workspaces, discovered tools, audit log.
By default that file is `.symbiote/symbiote.db` relative to the host process's
working directory, unless the host passes a custom `db_path`:

```python
kernel = SymbioteKernel(config=KernelConfig(db_path="data/symbiote.db"), llm=...)
```

To find an embedded database on disk: `find ~ -name "symbiote.db"`.

You can inspect or administer that database **without modifying the host app** —
just open the same file with one of these tools:

### Option A — Symbiote Console (visual admin UI)

The standalone server (`symbiote.api.http:app`) is just another reader of the
same SQLite file, and it serves the admin **Console** at `/`. Point it at the
embedded database with `SYMBIOTE_DB_PATH` and open it in a browser:

```bash
SYMBIOTE_DB_PATH=/path/to/host-app/.symbiote/symbiote.db \
SYMBIOTE_LOCAL_ADMIN=1 \
  uvicorn symbiote.api.http:app --host 127.0.0.1 --port 8008
# → open http://127.0.0.1:8008/
```

`SYMBIOTE_LOCAL_ADMIN=1` auto-provisions an admin key and injects it into the
Console — required for the detail/edit endpoints (e.g. opening a symbiote's
persona), which are auth-gated. Without it, an embedded DB has no API keys and
the editor cannot load (it surfaces an auth error instead of the persona). Only
use it on a Console bound to `127.0.0.1`.

The Console lists symbiotes, browses sessions/traces, **registers** symbiotes
(identity + persona + tools wizard), **edits** per-symbiote config, approves
discovered tools, and shows logs. `SYMBIOTE_DB_PATH` is read once by both
`get_adapter()` and `get_kernel()` in `api/http.py`, so the REST API and the
Console always open the same file.

### Option B — CLI

The CLI accepts a global `--db-path` flag (before the subcommand):

```bash
symbiote --db-path /path/to/host-app/.symbiote/symbiote.db list
symbiote --db-path /path/to/host-app/.symbiote/symbiote.db show <SYMBIOTE_ID>
symbiote --db-path /path/to/host-app/.symbiote/symbiote.db memory search "..."
symbiote --db-path /path/to/host-app/.symbiote/symbiote.db export session <SESSION_ID>
```

### Option C — raw SQLite

```bash
sqlite3 /path/to/host-app/.symbiote/symbiote.db ".tables"
```

### ⚠️ Concurrency caveat

SQLite is a single-file store. Opening the database from the server/CLI while
the host app is also writing to it is safe **for reads/inspection**, but
concurrent writes from two processes can hit `database is locked` errors. For
write operations (registering symbiotes, editing config), prefer doing them
while the host app is idle, or point the host app and the server at the same
file deliberately and accept the locking semantics.
