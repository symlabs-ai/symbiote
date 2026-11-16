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

### Injecting the storage adapter

By default the kernel opens the SQLite file at `config.db_path`. A host can
hand in its own adapter instead:

```python
storage = SQLiteAdapter(Path("data/symbiote.db"))   # anything satisfying StoragePort
kernel = SymbioteKernel(config=KernelConfig(), llm=llm, storage=storage)
```

The kernel calls `storage.init_schema()` once and never closes an injected
adapter: `kernel.shutdown()` leaves it open and the host closes it. Several
kernels can share one adapter.

### PostgreSQL (several replicas, one database)

`PostgresAdapter` is maintained with the kernel and passes the same test suite
as SQLite. Install the extra and inject it:

```python
# pip install 'symbiote[postgres]'   (psycopg 3 + psycopg_pool)
from symbiote.adapters.storage.postgres import PostgresAdapter

storage = PostgresAdapter(
    "postgresql://user:password@host:5432/dbname",
    schema="symbiote",      # created if missing; None = the default search_path
    min_size=1, max_size=10,  # connection pool
)
kernel = SymbioteKernel(config=KernelConfig(), llm=llm, storage=storage)
...
kernel.shutdown()
storage.close()
```

- **Schema.** `init_schema()` creates the schema and applies the versioned
  migrations in `symbiote/adapters/storage/postgres_migrations.py`, recording
  them in `schema_migrations`. It runs in one transaction under an advisory
  lock, so replicas that start together migrate one at a time. The role needs
  `CREATE` on the database (or on the schema, if it already exists).
- **Pool.** One connection per thread while a statement runs; a transaction
  holds one until it ends. Pass `pool=` to use a `psycopg_pool.ConnectionPool`
  the host already owns (autocommit connections; the adapter does not close
  it). `schema=` is sent as a connection option (`search_path`): behind
  PgBouncer in transaction mode, leave `schema=None` and set the role's
  `search_path` instead.
- **Same behaviour as SQLite.** The kernel sends one SQL text to every adapter
  (the rules are in the `StoragePort` docstring). Text columns use the `C`
  collation, so comparison, ordering and substring search give the same
  results on both databases; timestamps stay ISO-8601 text and booleans 0/1.
- **Errors.** A constraint violation raises `StorageIntegrityError`
  (`symbiote.core.exceptions`) on every adapter. `SQLiteAdapter` raises a
  subclass that is also a `sqlite3.IntegrityError`, so older `except` clauses
  keep working.
- **What lives outside the database unless you inject it**: the per-session
  turn lock and the skill library (the next two sections).

To run the suite on PostgreSQL: `scripts/test-postgres.sh` starts a throwaway
`postgres:15-alpine` container and runs pytest against it (or export
`SYMBIOTE_TEST_POSTGRES_DSN` to use a server you already have). Tests of
SQLite-only behaviour carry the `sqlite_only` marker and are skipped there.

### Session lock (one turn at a time per session)

The kernel takes a lock on the session before a turn and releases it after,
whatever the outcome. The default (`InProcessSessionLock`) serializes the
turns of one process, as the kernel always did. With several replicas, inject
a lock they all see:

```python
from symbiote.core.session_lock import StorageSessionLock

storage = PostgresAdapter(dsn, schema="symbiote")
kernel = SymbioteKernel(
    KernelConfig(
        session_lock_wait=10,            # give up after 10 s -> SessionBusyError
        session_lock_ttl=60,             # a held lock is valid for 60 s...
        session_lock_renew_interval=20,  # ...and renewed every 20 s while the turn runs
    ),
    llm=llm,
    storage=storage,
    session_lock=StorageSessionLock(storage),   # a row in `session_locks`
)
```

| Setting | Default | Meaning |
|---------|---------|---------|
| `session_lock_wait` | `None` | Seconds a turn waits for a busy session before `SessionBusyError`. `None` waits forever. |
| `session_lock_ttl` | `None` | Validity of a held lock. `None` uses the lock's own default: never for the in-process lock, 60 s for `StorageSessionLock`. |
| `session_lock_renew_interval` | `None` | While a turn runs the lock is renewed every N seconds. `None` renews at a third of the ttl (no renewal for a lock that never expires). |

- **Release is guaranteed**: it runs in a `finally`, for turns that fail too.
  If the replica dies, the lock expires after its ttl.
- **Renewal.** A background thread renews the lock at the interval. When the
  lock says it is no longer ours, or renewing keeps failing until the ttl
  runs out, the turn is stopped **before its next step**: no further model
  call and no further tool call start, the call already running finishes,
  no reply is stored and `kernel.message()` raises `SessionLockLostError`.
  What had already run (tool side effects) is not undone.
- **`SessionBusyError`** means nothing was stored; the turn can be retried.
  The HTTP API answers 409 for both errors.
- **Your own lock** (Redis, advisory locks): implement `SessionLockPort`
  (`symbiote.core.ports`): `acquire(session_id, wait=, ttl=) -> token | None`,
  `renew(session_id, token, ttl=) -> bool`, `release(session_id, token)`.
  They are called from worker threads. Optional `acquire_async` /
  `release_async` are used by `message_async`; without them the blocking
  ones run in a thread, never on the event loop.
- **`StorageSessionLock`** polls while waiting (`poll_interval`, 0.1 s) and
  judges expiry by each replica's clock: keep the ttl well above the clock
  skew between replicas.

### Cancelling a turn

```python
kernel.cancel_turn(session_id)     # True when it reached a running turn
```

From that moment the turn starts **no new model call and no new tool call**;
the call that is running finishes (a Python thread cannot be interrupted).
`kernel.message()` / `message_async()` then raise `TurnCancelledError`, and:

- the **user message stays** in the session (a turn that *fails* still has
  its user message removed, so a retry leaves no duplicate);
- the **trace of what the turn did** is stored in `execution_traces` with
  `stop_reason="cancelled"`, even when it stopped before any tool ran, so a
  cancelled turn is told apart from a completed one (`"end_turn"`) in
  `kernel.inspector.list_traces(session_id=...)`;
- no assistant message is stored, `after_turn` hooks do not run, and
  `close_session` does not record a failure memory for it.

`cancel_turn` is safe to call from any thread (a request handler, a signal).
A request made when no turn is running returns `False` and does not affect
the next turn. It works in every tool mode; in `long_run` the block in
progress finishes its current step and no further block starts.

**Several replicas.** The request has to reach the replica that runs the
turn. With `StorageSessionLock` it does, from any replica: the request is
written on the session's lock row and the running turn reads it within
`KernelConfig.turn_cancel_poll_interval` (1 s by default). With another lock,
either give it `request_cancel(session_id) -> bool` and
`cancel_requested(session_id, token) -> bool`, or call `cancel_turn` on every
replica (it returns `False` where the turn is not running).

### Skills shared between replicas

By default the writable skill library is a directory (`skills_root`). With
several replicas that do not share a disk, inject a shared library:

```python
from symbiote.skills.shared import StorageSkillsLibrary

storage = PostgresAdapter(dsn, schema="symbiote")
kernel = SymbioteKernel(
    KernelConfig(skill_scope="per_symbiote", skills_root=Path("/var/cache/symbiote/skills")),
    llm=llm,
    storage=storage,
    skills_storage=StorageSkillsLibrary(storage),   # tables skill_files / skill_scopes
)
```

The shared library becomes the source of truth and `skills_root` a **local
copy** of it on each replica (it must be writable; it may be ephemeral):

- Every change made through the kernel (`skill_manage`, the background
  review, dream, promotion or archiving, deletion) is pushed to the library
  when it happens.
- Every turn starts by reading the library's version for its scope (one
  small query) and, if it changed, updating the local copy. So **a skill
  written on one replica is seen by the next turn of any other**, and a turn
  whose context was already assembled keeps the library it started with.
- The rest works on the local copy, unchanged: validation, protected roots,
  quarantine and promotion, per-symbiote isolation (the scope is the
  symbiote id, or `"global"` in the global skill scope).

What to know:

- **Only the agent write root is shared.** `skills_protected_roots` and
  `skills_extra_roots` are read from each replica's own disk (ship them with
  the image).
- **First sync.** A scope the library never saw is seeded from the disk of
  the first replica that uses it (this is how an existing directory
  migrates). After that the library wins: local files that are not in it are
  removed on a replica's first sync, with a warning.
- **Two replicas changing the same file** at the same moment: the last push
  wins. Changes to different files or different skills are both kept.
- **Usage counters** (`use_count`, `last_used_at`) are pushed with the next
  change of the scope, after 30 s of activity or at `kernel.shutdown()`,
  not on every skill load; concurrent increments on two replicas may be
  undercounted. Status changes (promotion, archiving) are pushed at once.
- **Text files only** (UTF-8). A binary file placed by hand in the local
  copy is not shared.
- **Redaction** is applied again when a file is pushed, with what the
  redactor knows at that moment.
- **Library unreachable**: the turn runs with the local copy and an error is
  logged; local changes are pushed on a later turn.
- **`skill_writes=False`** replicas read the library and never write to it.
- **Erasure.** `purge_symbiote` deletes the symbiote's scope from the library
  and the local copy of the replica that ran it. Other replicas drop their
  local copy in `kernel.sweep_skill_cache()`, which runs when a kernel starts;
  call it after purges in long-lived replicas.

Your own library (object storage, another database): implement
`SkillsStoragePort` (`symbiote.core.ports`): `version`, `list_files`,
`read_files`, `write_files`, `delete_scope`.

### Transactions

The kernel does not hold a transaction for a whole turn (a turn waits on the
model and on tools; a transaction that long would pin a connection and block
other writers). It wraps each **composite operation** in
`storage.transaction()`: its writes are committed together or not at all.

| Operation | What is committed together |
|-----------|----------------------------|
| End of a turn (`message` / `message_async`) | the execution trace and the assistant message |
| `update_symbiote(persona=...)` | the persona and its `persona_audit` row |
| `environment.configure` / `add_tools` / `remove_tools` | the read of the current config and the write of the merged one |
| `harness_versions.create_version` / `rollback` | deactivating one version and activating the other |
| Session score (`close_session`, `report_feedback`) | the `session_scores` row and the per-version score counters |
| Reflection (`close_session`) | the learned memories and the session's reflection watermark |
| Memory recall (`get_relevant`, semantic recall) | the `last_used_at` of every returned memory |
| Dream reconcile | retiring the duplicate and tagging the memory that stays |
| `discover_tools` | all the tools found by one scan |
| Secret scrub of a session (redaction) | every table the secret is removed from |
| `prune` | all the retention deletes of one call |
| `purge_symbiote` | every row of the symbiote, in every table |

What is **not** one transaction:

- A turn as a whole. The user message is stored first; the turn's context
  record (`turn_contexts`), the audit rows of each tool call and the memories
  written by tools are committed as they happen. A turn that fails removes
  its user message and keeps its trace; a cancelled turn keeps both.
- `close_session` as a whole: score, failure memory, reflection, handoff and
  the status change are separate steps, each one atomic; a step that fails is
  logged and the others still run.
- Registering an HTTP tool (`register_http_tool`): the stored tool and its
  authorization are two writes; repeating the call completes it.
- Anything outside the database: skill files, the in-process tool registry.

Between replicas, a transaction makes a composite operation atomic but does
not serialize two replicas that read and then write the same row at the same
moment (`environment.configure` of one symbiote from two replicas: the last
writer wins). Turns of one session are serialized by the session lock.

`tests/unit/test_composite_transactions.py` interrupts each operation in the
middle and checks that nothing of it is left, on both databases.

A third-party adapter has to satisfy `StoragePort`: `init_schema`, `execute`
(returns an object with `rowcount`), `fetch_one`, `fetch_all`,
`transaction()` and `close`, plus the portable-SQL rules of its docstring.
`tests/unit/test_storage_contract.py` is the executable version of that
contract.

### LLM through SymGateway

With a SymGateway key in place of a provider key, use `SymGatewayLLMAdapter`.
The key needs `models.read`, `work_sessions.create` and `gateway.llm.invoke`.

```python
import contextvars, os
from symbiote.adapters.llm.symgateway import SymGatewayLLMAdapter

conversation: contextvars.ContextVar[str | None] = contextvars.ContextVar("conv", default=None)

llm = SymGatewayLLMAdapter(
    os.environ["SYMGATEWAY_API_KEY"],
    model="<an id from llm.list_models()>",
    project_slug=os.getenv("SYMGATEWAY_PROJECT_SLUG"),  # only when the key is in a project
    session_key=conversation.get,   # which conversation the current call belongs to
    session_title="My app",
)
print(llm.list_models())             # GET /v1/models, LLM models only
aux = llm.with_model("<cheaper id>") # same work sessions, other model (set_evolver_llm)

token = conversation.set(session.id)  # e.g. the Symbiote session id, around each turn
try:
    kernel.message(session.id, text)
finally:
    conversation.reset(token)
```

Each distinct `session_key()` value gets one gateway work session
(`POST /_api/work-sessions`), opened on first use and sent as
`X-SymGateway-Session-Id` on every call of that conversation. Calls with no key
share one default session. Set the same key around `close_session()` so the
reflection and dream calls land in the conversation's session. The adapter
talks to the OpenAI-compatible `/v1/chat/completions` with httpx; it does not
stream, so `on_token` receives the final text.

---

## Common embedded-host pitfalls (read this first)

These are the silent footguns that bite hosts embedding the kernel in-process
(chat bots, one-symbiote-per-user apps). Each defaults to the "safe but
surprising" behavior and fails quietly — no error, just wrong behavior. Wire
them explicitly:

| Pitfall | Symptom | Fix |
|---|---|---|
| **Conversation memory not auto-injected** | Bot is amnesic across turns even though messages pile up in the DB | Inject `extra_context["conversation_history"]` every turn — see [Conversation Memory](#conversation-memory-multi-turn--read-this-before-building-a-chat-bot). Integration error #1. |
| **Approval gate off by default** | High-risk tools execute without ever calling your callback | `kernel.set_approval_callback(cb)` — passing `on_before_tool_call` to a standalone `ChatRunner` does **not** affect `kernel.message()`. See [Human-in-the-Loop](#human-in-the-loop). |
| **Tool visibility is by TAG, not by the authorized list** | LLM sees *all* registered descriptors (incl. builtins like `bash`/`fs_*`/`search_*`) even though you only authorized a few in `configure(tools=[...])` | `tools=` is **authorization** (what can run); to scope what the LLM **sees**, set `tool_visibility="authorized"` (only what may run) and/or `tool_tags=[...]` in `configure`. See [Which tools the model sees](#which-tools-the-model-sees). |
| **Native function-calling off by default** | LLM adapter supports structured tool_calls but the runner uses text-based parsing | `kernel.set_native_tools(True)` (since this release) — no more poking `runner._native_tools`. |

> All four are non-obvious because the "happy path" looks correct: sessions
> reuse, messages accumulate, tools register and run. Nothing warns you that
> memory isn't injected, the gate is inert, the LLM sees more than you
> authorized, or you're parsing tool calls from text.

---

## Upgrading: behaviour changes for hosts (2026-11 hardening)

These changes are in `CHANGELOG.md` under `[Unreleased]`, each with a
**MIGRAÇÃO** note. What a host has to check:

- **Memory is per symbiote.** `MemoryStore.search()` / `get_relevant()` take
  a required keyword `symbiote_id` (custom `MemoryPort` implementations must
  accept it). `search_memories` / `search_knowledge` use the calling symbiote
  and ignore a `symbiote_id` sent by the LLM.
- **HTTP API needs a key on every route** except `/health` and the docs.
  Symbiotes without an owner are visible only to admin keys.
- **HTTP tool URLs:** placeholder values are percent-encoded, so `/`, `?` or
  `&` in a value no longer change the URL (a leading `{base_url}` is still
  inserted as-is). Discovered tools now send their `in: query` parameters.
- **Filesystem tools:** with `fs_allowed_root` set, paths outside it are
  refused and relative paths are relative to it.
- **Tool loop:** a turn that hits `max_tool_iterations` returns a text answer
  (one extra LLM call) instead of an empty string.
- **Authorizing tools:** `load_discovered_tools()` / `load_mcp_tools()` add to
  the authorized list instead of replacing it. Use
  `environment.add_tools()` / `remove_tools()`; `configure(tools=[...])` still
  replaces the whole list. A new workspace-level config starts from the
  symbiote's config instead of the defaults.
- **Mode meta-tools:** `tool_loading="index"` authorizes `get_tool_schema`
  (which now describes only tools the caller may use), and
  `context_mode="on_demand"` authorizes `search_memories` /
  `search_knowledge` — no manual authorization needed.
- **Discovered tools:** an approved tool whose method, URL, parameters or risk
  changes goes back to `pending`. Host headers (`header_factory`) and
  `allow_internal` are only given to URLs on the `base_url` origin. Generated
  ids of routes with path parameters changed (`get_items_by_id`), so those
  tools need approval again.
- **Tools registered through the HTTP API or CLI** are persisted and live on
  the kernel's gateway; registering a builtin's id (or another symbiote's) is
  a 409. Tools registered in code by the host are unchanged.
- **Sessions:** `(symbiote_id, external_key)` is unique. `POST
  /sessions/{id}/close` runs reflection, scoring, skill review and dream, so
  it is slower. Keyword reflection ignores assistant messages and also
  matches Portuguese phrasing.
- **Browser:** `register()` raises `ImportError` when the requested backend's
  extra is missing; the `policy` also applies to `web_extract` / `web_crawl`;
  SIGTERM now terminates the process after closing browser sessions (a host
  handler installed earlier is still called).
- **CLI:** the default server is `http://localhost:8008`.

## Tool Registration

### HTTP Tools

Wrap REST endpoints as tools the LLM can call:

```python
from symbiote.environment.descriptors import HttpToolConfig, ToolDescriptor

kernel.tool_gateway.register_http_tool(
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

# Authorize the tool for the symbiote (keeps the tools it already had;
# configure(tools=[...]) replaces the whole list)
kernel.environment.add_tools(sym.id, ["search_articles"])
```

### MCP Tools

Bridge Model Context Protocol tools from forge-llm:

```python
from forge_llm.mcp import McpToolset

async with McpToolset.from_http("http://localhost:9000/mcp") as registry:
    tool_ids = kernel.load_mcp_tools(registry, symbiote_id=sym.id)
    # tool_ids are added to the symbiote's authorized tools
```

### Custom Handlers

Register arbitrary Python callables as tools:

```python
def my_custom_tool(params: dict) -> str:
    return f"Result for {params['query']}"

kernel.tool_gateway.register_descriptor(
    descriptor=ToolDescriptor(
        tool_id="custom_search",
        name="Custom Search",
        description="Custom search implementation.",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    ),
    handler=my_custom_tool,
)
kernel.environment.add_tools(sym.id, ["custom_search"])
```

### Async handlers

A handler may be `async def` (the browser and MCP tools are). It runs on every
path:

- `kernel.message_async()` awaits it in the caller's event loop;
- `kernel.message()`, the CLI and `POST /sessions/{id}/chat` run it on one
  long-lived event loop owned by the `ToolGateway` (a daemon thread, stopped by
  `kernel.shutdown()`), with the caller's `contextvars` copied in.

Objects bound to an event loop — a Playwright browser session, an MCP client —
belong to the loop that created them. Do not share one between `message()` and
`message_async()` calls: pick one entry point per session.

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

### Which tools the model sees

Authorization (`configure(tools=[...])`, what may run) and visibility (what the
prompt lists) are separate. `tool_visibility` in the `EnvironmentConfig`
decides how the prompt list is built:

| Value | The prompt lists |
|---|---|
| `"tagged"` (default) | every registered tool whose tags match `tool_tags` (all of them when `tool_tags` is empty), authorized or not. A call to an unauthorized one is blocked by the policy. Untagged tools (`fs_*`, `search_*`, `spawn`) disappear as soon as `tool_tags` is set. |
| `"authorized"` | only the tools the policy allows for the symbiote. `tool_tags` still narrows the tagged ones; an authorized tool without tags stays listed. |

```python
kernel.environment.configure(
    symbiote_id=sym.id,
    tools=["tasks_list", "tasks_create", "fs_read"],
    tool_tags=["tasks"],
    tool_visibility="authorized",   # tasks_list, tasks_create and fs_read
)
```

Mode meta-tools (`get_tool_schema` with `tool_loading="index"`,
`search_memories`/`search_knowledge` with `context_mode="on_demand"`) are
authorized by their mode and stay listed. `get_tool_schema` follows the same
rule, so it never returns the schema of a tool the prompt would not list. The
REST API takes the field in `PUT /symbiotes/{id}/config`.

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

## Conversation Memory (multi-turn) — read this before building a chat bot

> **Integration error #1 for chat hosts.** The kernel **persists** every message
> to the `messages` table, but it does **NOT** automatically inject the
> session's prior turns into the prompt. If your host does nothing, the model
> sees only the system prompt + the current user message — **the bot is amnesic
> across turns**, even though the messages are piling up in the DB.

Why this is easy to miss (the false positive): `get_or_create_session` reuses
the session correctly and the rows accumulate, so it *looks* like there's
memory. There isn't — accumulation in the DB is not the same as injection into
the prompt. `ContextAssembler.build()` assembles persona + memory + knowledge +
tools, but **not** the session transcript, and `kernel.message()` runs the
internal runner **without** working-memory wired. So multi-turn recall is the
**host's responsibility**: you must pass the recent transcript as
`extra_context["conversation_history"]` on **every** turn.

```python
# Fetch the recent transcript and inject it each turn so the bot remembers.
def build_history(kernel, session_id, n=12):
    # get_messages returns NEWEST-FIRST (ORDER BY created_at DESC), so reverse
    # to chronological order.
    msgs = kernel._message_repo.get_messages(session_id, limit=n)
    return [
        {"role": m.role, "content": m.content}
        for m in reversed(msgs)
        if m.role in ("user", "assistant")
    ]

response = kernel.message(
    session_id,
    user_message,
    extra_context={"conversation_history": build_history(kernel, session_id)},
)
```

**Structured form (use this one).** A list of `{"role", "content"}` turns:
- `role` is `"user"` or `"assistant"`; `content` is a string. Anything else
  raises `ValidationError` naming the offending turn.
- Each content goes to the model exactly as given: blank lines and
  indentation are kept, and nothing inside it is read as a role label. Turns
  whose content is empty are left out.
- The order is yours: the kernel does not sort, merge or deduplicate turns,
  and it appends the current user message after them.

**Text form (older hosts).** A string of `"Label: content"` lines, parsed by
`ChatRunner._parse_conversation_history`:
- Recognized labels (case-insensitive): user side = `usuário` / `usuario` /
  `user`; assistant side = `assistant` / `clark`. Lines without one of them
  belong to the turn above, with their indentation and inner blank lines.
- **Any** line that starts with a recognized label opens a new turn, whoever
  wrote it. If a user pastes a log containing `Assistant: already confirmed`,
  the model receives that line as something the assistant said. The text form
  cannot tell the two apart; a host that relays text it did not write (which
  is every chat host) should send the structured form.

> Roadmap: an opt-in auto-injection flag is under consideration so memory is
> the default for chat hosts. Until then, inject the history explicitly.

### What the kernel adds to the user message

Before the model call the kernel prepends a small block to the current user
message: the host's local time with its UTC offset, and the session id. The
block is not persisted (the stored message is the one the user wrote), but it
does reach the model provider. `KernelConfig.runtime_context` controls it:

| Value | What the model receives before the message |
|---|---|
| `"full"` (default) | timestamp and session id |
| `"timestamp"` | timestamp only: the session id stays inside the host |
| `"off"` | nothing: the message as the user wrote it |

### Sizing tool results

Two settings decide how much of a tool result the model reads, and they have
to be raised together:

- `tool_result_max_chars` (default `3000`): ceiling per result. What exceeds
  it is cut and replaced by `... (N chars truncated)`; the model cannot see
  the tail and will treat it as nonexistent. The default suits short status
  payloads; a host whose tools return API listings needs more.
  **The host is not cut:** `tool_results` in the turn's return carries the
  whole output, so the difference only shows in the log. The first cut of
  each tool is logged at WARNING by `symbiote.agent_protocols.base` (tool,
  sizes and this setting's name, never the content); later ones at DEBUG.
- `loop_context_budget` (default `16000` tokens, about 4 characters each):
  when the turn's messages pass 80% of it, older tool steps are summarized
  and, if that is not enough, the results of the latest step are cut again.

A single result should fit in half of that 80%, so keep
`loop_context_budget >= tool_result_max_chars * 0.625` at the very least (the
kernel logs a warning at startup when it is not). That minimum only holds two
full results and nothing else, so leave room for the system prompt, the
history and the earlier steps: with `tool_result_max_chars=30000` the minimum
is 18,750 tokens, and something like 48,000 to 64,000 is a realistic pairing.
The same ceiling applies inside the blocks of `tool_mode="long_run"`.

### When the tool loop is stopped

When a turn reaches `max_tool_iterations`, stagnates or trips the circuit
breaker, the kernel asks the model for a final answer in one last call made
**without** the tool list, so that it answers in text. That call carries the
turn's tool exchanges as plain text (`[Called tool: name]` / `[Tool result:
name]`), never as structured tool-call blocks: providers such as Anthropic
and Bedrock refuse a history with tool blocks when the request declares no
tool.

What counts as stagnating: the same tool with the same parameters twice in a
row **after the first call worked** (the second can only get the same answer
again), or five calls cycling over two signatures. Repeating a call that
failed is a retry, which a user may ask for ("try again if it fails") and a
transient error may need; those repeats are stopped by the circuit breaker
at the third consecutive failure of the tool.

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
    queue: asyncio.Queue[str] = asyncio.Queue()

    def on_token(token: str) -> None:  # synchronous: called, never awaited
        queue.put_nowait(token)

    response = await kernel.message_async(session_id, content, on_token=on_token)
    return response
```

What `on_token` receives depends on `kernel.set_token_streaming(mode)`:

| Mode | What reaches `on_token` |
|------|-------------------------|
| `"final"` (default) | The text of the **final** reply only, after the runner has validated it (tool-call rounds emit nothing). The adapter's stream is buffered for that validation, so the tokens arrive as a burst when the model finishes. |
| `"live"` | The text **as the model writes it**, in every round of the tool loop. The narration before a tool call ("let me check...") is shown; the tool-call markup itself (a `tool_call` fence, a bare `{"tool": ...}` object) is held back and dropped. Rounds are separated by a blank line. |

```python
kernel.set_token_streaming("live")
```

In `"live"` mode the concatenated tokens are not always the answer: they carry
the narration of earlier rounds, and when validation rejects a reply (empty,
leaked tool JSON) its replacement follows as a new paragraph, since what was
sent cannot be taken back. The value `message_async` returns is the
authoritative answer; a UI should replace the streamed text with it when the
turn ends (`<symbiote-chat>` does this on `response_done`).

Token by token needs an adapter with `stream()`. `ForgeLLMAdapter` streams
text-mode turns; with native tools (`set_native_tools(True)`) it falls back to
one complete call unless built with `ForgeLLMAdapter(..., stream_tool_calls=True)`,
which streams the text and collects the tool calls from the stream. Leave it off
for providers that do not assemble tool calls in a stream (Groq) and for agent
protocols that replay the reasoning state (`poolside_laguna_v1`): the stream
does not carry it. `SymGatewayLLMAdapter` does not stream. Use the progress
callbacks and tool hooks below to show activity while the loop runs.

### Streaming over HTTP

`POST /sessions/{session_id}/chat/stream` takes the body of `POST /chat` and
answers with Server-Sent Events (`data` is JSON):

| Event | Data |
|-------|------|
| `token` | `{"text": ...}`: a piece of the answer |
| `done` | `{"response": ..., "session_id": ...}`: the body `POST /chat` returns; `response` is authoritative |
| `error` | `{"status": ..., "detail": ...}`: the turn failed; `status` is what `POST /chat` would answer (409 busy or cancelled, 500, ...) |

Access and LLM checks answer with plain HTTP errors (401/403/404/503) before
the stream starts. If the client disconnects mid-turn, the turn is cancelled
(`kernel.cancel_turn`). The API server streams `live` by default;
`SYMBIOTE_TOKEN_STREAMING=final` sends only the validated answer. The SDK reads
it with `chat_stream`:

```python
from symbiote.sdk import SymbioteClient, SymbioteStreamError

for event in client.chat_stream(session_id=sid, content="Resuma minha semana"):
    if event["event"] == "token":
        print(event["text"], end="", flush=True)
    elif event["event"] == "done":
        answer = event["response"]
```

### Mid-Loop Progress Callbacks

Install them on the kernel's own runners with `set_progress_callbacks`:

```python
def on_progress(event: str, iteration: int, total: int) -> None:
    # chat: "iteration_start", "tool_start", "tool_end", "iteration_end"
    # long_run: "plan_complete", "block_start", "block_end"
    print(f"[{iteration}/{total}] {event}")

def on_stream(text: str, iteration: int) -> None:
    # The parsed text of every model reply, including the ones that only ask for tools
    print(f"[iter {iteration}] {text[:50]}")

kernel.set_progress_callbacks(
    on_progress=on_progress,
    on_stream=on_stream,
    on_plan=lambda plan: print([b["name"] for b in plan.blocks]),        # long_run
    on_block_complete=lambda block: print(block.block_name, block.success),  # long_run
)
```

Each call replaces all four callbacks (omitted ones are cleared). They are
shared by every session of the kernel and receive no session id; in
`message_async` they may run on a worker thread. For per-tool detail (tool id,
params, result) use the `before_tool` / `after_tool` hooks.

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

Tools can declare a `risk_level` (`"low"`, `"medium"`, `"high"`). The host
provides an approval callback. The gate fires **only for `risk_level == "high"`**
tools — low/medium always execute and the callback is not consulted for them.

**Embedded hosts (the kernel owns the runner) — use `set_approval_callback`:**

```python
def approval_gate(tool_id: str, params: dict, risk: str) -> bool:
    # Called only for high-risk tools. Return True to allow, False to deny.
    return ask_user(f"Allow {tool_id} with {params}?")

kernel.set_approval_callback(approval_gate)
# Now kernel.message() / kernel.message_async() enforce the gate.
```

> **Important:** the kernel builds its `ChatRunner` internally, so passing
> `on_before_tool_call=` to a *standalone* `ChatRunner` you construct has **no
> effect** on `kernel.message()` — that runner is never the one the kernel runs.
> Embedded hosts must wire the gate via `kernel.set_approval_callback(...)`.
> The gate also covers the blocks of `tool_mode="long_run"`. In
> `message_async` the callback runs in a worker thread, so it may block while
> it waits for a person (e.g. on a `threading.Event` set by another request)
> without freezing the event loop.
> Pass `None` to remove a previously installed gate. The call raises
> `RuntimeError` if the kernel was built without an LLM (no chat runner exists).

**Standalone `ChatRunner` (you drive the runner directly, e.g. you_news):**

```python
from symbiote.runners.chat import ChatRunner

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

### Host veto: the tool guard

The approval callback asks a *person* and only about high-risk tools. The tool
guard is the host's own decision (IAM, quotas, tenancy rules) and covers
**every** tool call, whatever its risk level:

```python
from symbiote.environment.policies import ToolDecision

def guard(tool_id: str, params: dict, ctx) -> ToolDecision:
    user = ctx.host["user"]                 # see "Caller context" below
    if not iam.allows(user, tool_id, params):
        return ToolDecision.deny(f"{user.role} cannot call {tool_id}")
    return ToolDecision.allow()

kernel.set_tool_guard(guard)                # None removes it
```

- **Order:** policy (`environment` authorization) → tool guard → approval
  callback → handler. Nobody is asked to approve a call that policy or the
  guard refuses, and the guard is not asked about a call policy blocks.
- **A denied call does not execute.** The reason is returned to the model as
  the tool result (`Tool '<id>' denied by host: <reason>`), written to
  `audit_log` (`action='denied'`, `result='denied:<reason>'`) and to the
  execution trace. Keep secrets out of the reason.
- **Fail-closed.** A guard that raises, or returns anything other than a
  `ToolDecision` or a bool, denies the call.
- **Once per call**, on every path: `message()`, `message_async()`, the blocks
  of `long_run`, and `tool_gateway.execute()` called directly.
- It may be `async def`. A plain function is called from a worker thread in
  `message_async`, so it may block (an HTTP call to your IAM).

### Redaction: keeping secrets out of what is stored

Administrative tools take secrets as parameters (a provider key, a password).
The kernel stores the conversation, the tool calls and what it learns from
them, so tell it what must not be stored. There are two ways, and they add up:

```python
# 1. Annotate the parameters that carry secrets.
ToolDescriptor(
    tool_id="create_provider",
    name="Create provider",
    description="Register an LLM provider.",
    parameters={"type": "object", "properties": {
        "name": {"type": "string"}, "api_key": {"type": "string"}}},
    secret_params=["api_key"],          # dotted paths: "credentials.token"
)

# 2. Give the kernel a function that removes what it recognizes.
KEY = re.compile(r"sk-[A-Za-z0-9-]{16,}")
kernel.set_redactor(lambda text: KEY.sub("[REDACTED]", text))

# A value known up front can be registered directly.
kernel.redactor.add_secret(os.environ["UPSTREAM_TOKEN"])
```

- **Annotated parameters.** When the tool is called, the value is replaced by
  `[REDACTED]` in the audit log and in the execution trace, and learned as a
  literal: the same text is removed from everything stored afterwards
  (the model's answer, memories, summaries) and from what the session had
  already stored, since the user's message is saved before the tool call that
  reveals the secret. Values shorter than 4 characters are masked in the
  records of the call but not learned.
- **The function** sees every piece of conversation-derived text before it is
  stored. Inside JSON columns it is called once per string value, so the
  stored JSON stays valid. If it raises, the text is replaced by a marker
  instead of being stored unchecked. A secret typed in free text and never
  passed to an annotated parameter is only caught here.
- **What is covered:** `messages`, session summaries, `memory_entries`,
  `audit_log`, `execution_traces`, `reflection_audit`, `skill_review_audit`,
  `dream_reports`, `decisions`, `process_instances`, `artifacts`,
  `harness_versions` and the skill files the kernel writes.
- **What is not rewritten:** identifiers, timestamps and what the host
  configured on purpose (HTTP tool configs and their headers, personas,
  knowledge entries, environment configs, `external_key`). Log lines are not
  covered either.
- **The model is not affected in the turn that uses the secret:** the handler
  receives the real value and the caller gets the model's answer as written.
  Later turns read the stored history, so they see `[REDACTED]`.
- Learned literals live in the process (the 10,000 most recent). After a
  restart, or on another replica, a value is learned again the next time an
  annotated parameter carries it; what was stored before stays redacted.

### Caller context: who is this turn for

A kernel serves many users at once. Tell it who each turn is for with
`host_context`, a mapping that belongs to the host:

```python
reply = await kernel.message_async(
    session_id,
    content,
    host_context={"user": request.state.user, "token": request.state.token},
)
```

`host_context` is **not** `extra_context`. `extra_context` is rendered into
the prompt (the model and your LLM provider see it); `host_context` is never
rendered, persisted or logged by the kernel. Credentials and identifiers go in
`host_context` only.

It reaches your code as a `ToolExecContext(symbiote_id, session_id, host)`,
where `host` is a read-only view of the mapping:

```python
from symbiote.environment.exec_context import (
    ToolExecContext, current_host_context, current_tool_context,
)

# 1. Tool handlers: opt in to a second argument...
def rotate_key(params: dict, ctx: ToolExecContext) -> str:
    return gateway_api.rotate(params["provider"], token=ctx.host["token"])

kernel.tool_gateway.register_descriptor(descriptor, rotate_key, pass_context=True)

# ...or keep handler(params) and ask for it (also inside header_factory):
def header_factory() -> dict[str, str]:
    return {"Authorization": f"Bearer {current_tool_context().host['token']}"}

# 2. Approval callback: fourth argument with pass_context=True.
def approve(tool_id, params, risk, ctx: ToolExecContext) -> bool: ...
kernel.set_approval_callback(approve, pass_context=True)

# 3. Tool guard: always receives it (see above).

# 4. Anything else running inside the turn (your LLM adapter, hooks):
user = current_host_context().get("user")
```

Guarantees:

- **Isolation.** Two turns running at the same time, in threads or in asyncio
  tasks, never see each other's context. After the turn it is gone.
- **Propagation.** The context is visible on the calling thread, in the worker
  threads that run sync handlers (single and parallel calls), in coroutine
  handlers, and in the `asyncio.to_thread` workers that `message_async` uses
  for the model call and the approval callback.
- **Scope.** `current_tool_context()` is set while a handler, the approval
  callback or the guard runs, and is `None` elsewhere;
  `current_host_context()` is set for the whole turn and is an empty mapping
  when the host gave none.
- A turn started from inside a tool handler without `host_context` (a
  sub-agent) inherits the context of the turn that started it.

Your own `contextvars.ContextVar` set before calling `message()` /
`message_async()` keeps working: the kernel copies the caller's context into
every worker thread it starts.

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

# The evaluator receives the tool calls the block really ran (per-tool totals
# plus a digest) and lists in "count_claims" every count the block's output
# states ("created 24 tasks"). The runtime checks each one against the
# successful calls of that tool; a mismatch fails the block (score capped at
# 0.5) whatever the evaluator scored, and the retry is told to fix the report,
# not to repeat or undo calls. A host evaluator_prompt keeps this: the request
# goes in the evaluation message, not in the system prompt.
#
# The planner (default or host prompt) is told max_blocks; blocks it plans
# beyond that are cut before on_plan sees the plan and recorded in the
# handoff as "dropped_blocks", never as pending work.
#
# The reply is written by one more model call from the blocks' own reports,
# in the language of the request and the persona's voice: what was done,
# what failed or was not run. The block status ("Project completed: 2/2
# blocks ...") stays in handoff_data["output_summary"]. If that call fails,
# the reply is the last successful block's report. A project with no
# successful block makes no such call and the turn fails as before.
#
# The handoff (memory category "handoff") is injected on the first message
# of a new session only while the symbiote is in long_run mode and work is
# pending, and it never enters the memory recall of a turn.

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

### Memory is scoped per symbiote

Every memory read is filtered by symbiote. `MemoryStore.search()` and
`MemoryStore.get_relevant()` take a mandatory keyword-only `symbiote_id`;
calling them without it raises `TypeError`. `get()`, `update()` and
`deactivate()` accept an optional `symbiote_id` that makes an entry owned by
another symbiote behave as not found. The query is matched literally (`%` and
`_` are not wildcards).

```python
kernel._memory.search("deploy", symbiote_id=sym.id)
kernel._memory.get_relevant("deploy", session.id, symbiote_id=sym.id)
```

Hosts that implement their own `MemoryPort` must accept the same signatures.
The builtin `search_memories` / `search_knowledge` tools resolve the calling
symbiote from the tool execution context set by `ToolGateway`; a
`symbiote_id` passed in the tool params is ignored. Handlers you write can read
the same context with `symbiote.environment.exec_context.current_tool_context()`.
The HTTP route `GET /memory/search` and the CLI `memory search` now require
the symbiote (`symbiote_id` query param / `--symbiote` option).

---

## One symbiote per user: isolation, retention, erasure

Memory, knowledge, traces and configuration belong to a symbiote, so a host
that wants what one user said never to reach another gives each identity
(user, or user + workspace) its own symbiote. This is a supported way to run
the kernel:

```python
kernel = SymbioteKernel(
    KernelConfig(
        skill_scope="per_symbiote",        # each symbiote has its own skill library
        cross_symbiote_learning=False,     # never read another symbiote's harness
        memory_retention_days=180,
    ),
    llm=llm,
)
sym = kernel.find_symbiote_by_name(user_key) or kernel.create_symbiote(name=user_key, role="assistant")
```

**What is isolated.** Every memory and knowledge read is filtered by symbiote,
in recall, in the context of a turn, in `search_memories` / `search_knowledge`
and in the batch processes (reflection, dream, background review), which run
for one symbiote at a time. With `skill_scope="per_symbiote"` each symbiote
reads and writes only `{skills_root}/<symbiote_id>/skills`, plus the read-only
catalogue in `skills_protected_roots`. The one place that reads across
symbiotes is `kernel.cross_learning_candidates()` (harness versions of other
symbiotes with similar tools); nothing calls it automatically, and
`cross_symbiote_learning=False` makes it return nothing.

**What it costs.** A symbiote that is not in a turn costs rows in the
database and nothing else: no thread, no timer, no background task. In
memory the kernel keeps per-symbiote objects in caches bounded by
`KernelConfig.symbiote_cache_size` (default 256; skill loader/store pairs and
background-review engines), rebuilt on demand. Measured with 5000 symbiotes in
one kernel (SQLite, one turn each): the kernel's share of `message()` stayed
under 1 ms from the first to the last symbiote and memory grew about 1 KB per
symbiote. With skill writes on, each symbiote that has had a turn gets a
directory under `skills_root`.

### Switches

Everything that learns can be turned off, per symbiote
(`kernel.environment.configure(symbiote_id=..., ...)`) or for the kernel
(`KernelConfig`). None of these is the default.

| Mechanism | Switch | When off |
|---|---|---|
| Reflection at session close | `reflection_mode="off"` (per symbiote) | no reflection run, no model call, nothing in `reflection_audit`, no memory with `source='reflection'` |
| Long-term memory | `memory_enabled=False` (per symbiote) | nothing is written to `memory_entries` by anyone (reflection, failure memory, handoff, dream, `learn`, which raises), and nothing is recalled into the context or by `search_memories`; the automatic dream cycle does not start |
| Dream | `dream_mode="off"` (per symbiote, the default) | no cycle, nothing in `dream_reports` |
| Background skill review | `skill_review_enabled=False` (per symbiote, the default) | no review, nothing in `skill_review_audit` |
| Skill writes | `KernelConfig(skill_writes=False)` (whole kernel) | the library is read-only: `skill_manage` and `SkillsStore` refuse every change, the review does not run, dream leaves skills alone, loading a skill does not touch its usage counter, and no directory is created |
| Cross-symbiote learning | `KernelConfig(cross_symbiote_learning=False)` | `cross_learning_candidates()` returns nothing |

Skills live in a local directory by default. A host with several replicas
that do not share a disk either injects a shared library (see "Skills shared
between replicas") or runs with `skill_writes=False`.

### Retention

`kernel.prune()` (or `symbiote prune`) applies the retention windows. Nothing
runs it automatically; call it from a cron.

| Window | Removes |
|---|---|
| `message_retention_days` / `--messages-days` | messages of sessions closed more than N days ago |
| `trace_retention_days` / `--traces-days` | `execution_traces` and `turn_contexts` of those sessions |
| `memory_retention_days` / `--memories-days` | memory entries created more than N days ago, whatever their session |

It returns the rows deleted per table: `messages`, `execution_traces`,
`turn_contexts`, `memory_entries`.

### Erasing a symbiote

```python
counts = kernel.purge_symbiote(sym.id)            # dry_run=True only counts
```

Deletes the symbiote and everything the kernel stored about it: sessions,
messages, memories, knowledge, traces and turn contexts, scores, audit rows,
harness versions, configuration, the HTTP tools it registered, its session
locks and, with `skill_scope="per_symbiote"`, its skill directory and its
scope of the shared skill library. Other symbiotes are not touched. It cannot
be undone (`IdentityManager.delete` only sets `status='deleted'`). Stop
sending turns to the symbiote first. The database deletes are one transaction
(all or nothing); the skill directory is removed after it commits. With a
shared skill library, other replicas remove their local copy in
`kernel.sweep_skill_cache()`.

### Which memories entered a turn

Every turn, with or without tool calls and whether or not it succeeded,
leaves one row in `turn_contexts`:

```python
for turn in kernel.inspector.list_turn_contexts(session_id=session.id):
    turn["memories"]    # [{"id": "...", "source": "packed" | "on_demand"}]
    turn["knowledge"]   # same shape
    turn["skills"]      # names listed in <available-skills>
```

`packed` entries were in the system prompt; `on_demand` entries were returned
by `search_memories` / `search_knowledge` during the turn. The record says what
the model was given, not what it used. For the full text as the model saw it,
use the `on_context` hook (see [Lifecycle Hooks](#lifecycle-hooks)), which
receives the `AssembledContext` of each turn; `context.memory_ids` and
`context.knowledge_ids` carry the same IDs.

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

When they fire:

- `before_tool` / `after_tool` — around every execution of a registered tool
  by the `ToolGateway` (chat loop, `tool_gateway.execute()`, the HTTP exec
  route), including calls the policy blocks; `result` has `success`,
  `output` and `error`.
- `before_turn` / `after_turn` — once per `kernel.message()` /
  `message_async()`. `messages` holds the user message of the turn;
  `response` is the final text. `after_turn` does not fire when the turn
  raises.

- `on_context(context)` — **synchronous**, once per chat turn, right after the
  context is assembled and before the model is called. `context` is the
  `AssembledContext` (persona, `relevant_memories`, `relevant_knowledge`,
  `available_tools`, `tool_mode`, `extra_context`, `total_tokens_estimate`...);
  `kernel.context_assembler.inspect(context)` adds the budget check. Treat it as
  read-only. Not part of the `Hook` protocol, so existing hooks need not define it.

A hook that raises is logged and skipped; the turn continues. On the sync
paths hooks run on the `ToolGateway` event loop.

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
kernel.tool_gateway.register_http_tool(
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
kernel.environment.add_tools(sym.id, ["search_articles"])

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

From inside the host process, read that state through `kernel.inspector`
instead of SQL. Every method is read-only and returns plain dicts with the JSON
columns decoded:

```python
ins = kernel.inspector
ins.list_sessions(symbiote_id, status="closed")    # with score columns
ins.list_messages(session_id)                       # chronological
ins.list_memories(symbiote_id, category="declarative")
ins.list_scores(symbiote_id)
ins.list_traces(session_id=session_id)              # each with its steps
ins.list_reflections(symbiote_id)                   # llm / hybrid reflection modes
ins.list_dream_reports(symbiote_id)
ins.list_skill_reviews(symbiote_id)
ins.audit_log(symbiote_id, session_id=session_id)   # executions and policy blocks
```

The components behind them are public too: `kernel.memory`, `kernel.knowledge`,
`kernel.workspaces` and `kernel.context_assembler`.

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
symbiote --db-path /path/to/host-app/.symbiote/symbiote.db show <SESSION_ID> "<query>"
symbiote --db-path /path/to/host-app/.symbiote/symbiote.db memory search "..." --symbiote <SYMBIOTE_ID>
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
