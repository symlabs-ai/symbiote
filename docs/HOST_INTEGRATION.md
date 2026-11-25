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
| Memory recall (`get_relevant`, semantic recall, `get_standing`) | the `last_used_at` of every returned memory |
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

SymGateway is optional. The kernel calls whatever `LLMPort` the host gives it
and needs nothing from a gateway: with a provider key, use `ForgeLLMAdapter`
(or your own adapter) and skip this section. `ForgeLLMAdapter` sends the
gateway's project (`project_slug`, default `"symbiote"`) only when
`provider="symgateway"`; pass `project_slug="<yours>"` or `None` to change it.

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
talks to the OpenAI-compatible `/v1/chat/completions` with httpx. By default
it sends one request and reads one whole reply, so `on_token` receives the
final text; `streaming=True` changes that (below).

The gateway can also choose the model: name an Objective instead of a model.

```python
llm = SymGatewayLLMAdapter(api_key, objective="chat_fast", on_routed=print)
llm.list_objectives()                 # the key's LLM Objectives (needs objectives.read)
aux = llm.with_objective("summarize") # same work sessions, routed by another Objective
llm.complete(messages, config={"objective": "summarize"})  # for one call
llm.last_routing   # {"model": ..., "objective": ..., "provider": ..., "request_id": ..., "work_session": ...}
```

`on_routed(routing)` is called after every call with what the gateway
answered, including the Objective that routed it. Objectives exist only in
this adapter; the kernel and `LLMPort` know nothing about them.

**Streaming.** Built with `streaming=True`, the adapter asks the gateway for
every reply as an event stream and has a `stream()`:

```python
llm = SymGatewayLLMAdapter(
    api_key,
    model="<id>",
    streaming=True,
    on_activity=note_activity,  # optional: runs for every line of a stream
    timeout=420.0,              # with streaming: the longest silence inside a reply
)
```

- `on_token` receives the text in the pieces the model wrote: as it writes
  them with `set_token_streaming("live")`, and once the reply is validated
  in the default `"final"` mode (see "Streaming"). With native tools too:
  the tool calls are put together from the stream and come as one
  `LLMResponse` after the text, as `ForgeLLMAdapter(stream_tool_calls=True)`
  does.
- `complete()` reads the stream as well and returns what it returns without
  the option. A reply that takes minutes arrives over a connection that
  stays active, instead of one silent wait for the whole response.
- `on_activity()` runs for every line received, the empty sign-of-life
  lines included, on the thread that makes the call. How often to tell
  anyone is the host's matter: keep the time of the last line and compare.
- A refused request raises `LLMError` with its status (`retryable` for 408,
  429 and 5xx), as without streaming. A stream that is not a whole reply (a
  chunk that carries `error` or is not JSON, a connection that ends before
  `finish_reason` or `[DONE]`) and a timeout raise `LLMError(retryable=True)`:
  the kernel asks again.
- Token usage is the `usage` of the last chunk (the adapter sends
  `stream_options: {"include_usage": true}`).
- `session_key`, the replacement of a rejected work session and `on_routed`
  work as before. The `objective`, `provider` and `request_id` of a streamed
  call's routing come from the gateway's `symgateway` block when a chunk
  carries it, and are `None` when none does; `model` and `work_session` are
  always there.

Without `streaming=True` the adapter has no `stream` at all
(`hasattr(llm, "stream")` is `False`): the kernel keeps calling `complete()`,
and the request body is the one it always was.

**Provider options.** The gateway forwards `provider_options` to the
provider behind it (Anthropic's prompt cache, how much the model thinks).
The adapter carries them; which model takes which option is the host's
decision, and `models()` says which provider serves each model:

```python
llm = SymGatewayLLMAdapter(api_key, model=model)
entry = next((m for m in llm.models() if m.get("id") == model), {})
if entry.get("provider") == "anthropic":
    llm.provider_options = {"cache_control": {"type": "ephemeral"}}

aux = llm.with_model("<another provider's id>", provider_options=None)
llm.complete(messages, config={"provider_options": {"output_config": {"effort": "high"}}})
```

- `provider_options=` at construction, or the attribute set later, goes in
  the body of every call. Empty, the default, sends no such field.
- The `provider_options` of one call's config (the `llm_config=` of a turn)
  is merged over the adapter's, key by key at the first level.
- `with_model()` and `with_objective()` keep the options unless
  `provider_options=` is given: pass the other model's own, or `None`, so an
  auxiliary model of another provider does not inherit `cache_control`.
- `models()` returns the entries of `GET /v1/models` as the gateway sends
  them, of every service type; `list_models()` is still the ids of the LLM
  ones.

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

### Upgrading to 0.8.10

The database gains migrations 13 to 15 (PostgreSQL 7 to 9), applied when the
kernel starts. Every new option defaults to the old behaviour. What a host
may notice:

- **Score:** a score given with `report_feedback` before `close_session` is
  now part of `final_score` (it used to be overwritten by the close).
- **Tool results:** `ToolCallResult`, and the `tool_results` dicts of
  `message()`, have a new key `pending_approval_id` (`None` unless the call was
  deferred).
- **`prune()`** returns one more key, `llm_usage`.
- **Turn contexts:** the rows of `inspector.list_turn_contexts` have
  `llm_calls`, `prompt_tokens`, `completion_tokens` and `total_tokens`.
- **Web tools:** `register(search_routing=...)` with no provider for
  `web_search` raises `ValueError` (it used to fall back to Brave through
  SymGateway). An unknown backend name raises `ValueError`, not
  `NotImplementedError`.
- **`ForgeLLMAdapter`** no longer sends `project_slug` to providers other than
  `symgateway`.
- **Input tokens with Anthropic:** `prompt_tokens` is now the whole input,
  prompt cache included, on every usage record. Through `ForgeLLMAdapter` the
  cached part used to be left out, so for hosts that use prompt caching the
  input count (and `total_tokens`) goes up. A host that prices from
  `prompt_tokens` must take the cache out: the input at the ordinary rate is
  `prompt_tokens - cache_read_tokens - cache_write_tokens`. OpenAI is
  unchanged. `ForgeLLMAdapter` also asks every streamed call for its usage.
- **Private attributes:** `kernel._llm` and `kernel._evolver_llm` are now a
  metering wrapper around the adapter you passed (`.wrapped` is your adapter,
  and its attributes stay reachable through the wrapper). `current_spawn()`
  and `wait_dream()` replace reading `_spawn_depth` and `_dream_threads`.

---

### Upgrading to 0.8.11

Migrations 16 and 17 (PostgreSQL 10 and 11). Nothing changes for a host that
uses neither the spending ceiling nor revisions. What a host may notice:

- `stop_reason` has a new value, `budget`, only for symbiotes that set a
  ceiling.
- Sessions, `inspector.list_sessions`, `list_turn_contexts` and
  `list_llm_calls` rows have a `revision` key (`None` unless the session is
  pinned).
- `EnvironmentManager.get_config` (and every `get_*` of it) returns the
  config of the revision inside a turn of a pinned session;
  `current_config` always returns what is stored now.
- `POST /sessions` goes through the kernel: an unknown `symbiote_id` is a 404
  as before, and `revision` is checked.

### Upgrading to 0.8.12

Migration 18 (PostgreSQL 12). Nothing changes for a host that does not narrow
a session's tools, set a default revision or ask for strict delegation. What a
host may notice:

- Sessions and `inspector.list_sessions` rows have the keys `tools_allowed`
  (`None`: no restriction) and `delegation` (`"open"`); revisions and
  `inspector.list_revisions` rows have `is_default`.
- A call that waited for a person (`resolve_pending`) now runs as the session
  that asked: under the revision that session is pinned to, and within the
  tools it is narrowed to at that moment. Before, policy was checked against
  the current configuration even for a pinned session.
- `import symbiote.discovery` now exposes `parse_openapi`, `http_config_for`
  and `OpenAPIOperation`.

### Upgrading to 0.8.13

No migration. Without the new retention windows set, `prune()` deletes
exactly what it deleted before. What a host may notice:

- **`prune()`** returns more keys: one per table it can touch (`audit_log`,
  `reflection_audit`, `skill_review_audit`, `pending_approvals`, `decisions`,
  `process_instances`, `artifacts`, `session_locks`, `session_scores`), plus
  `summaries` and `sessions`. Code that compares the whole result with a
  fixed dictionary needs the new keys.
- **If you remove messages for privacy**, check the session summaries: the
  message window never removed them, and the default summary is the text of
  the session's last five messages. Set `summary_retention_days`, or
  `session_summary_mode="off"` so none is written. See
  [Retention](#retention).
- **If you keep one session open per conversation**, the message and trace
  windows never reached it. `retention_basis="age"` makes them apply by the
  age of each row.

### Upgrading to 0.8.15

Migration 19 (PostgreSQL 13): the column `messages.origin`. Nothing changes
for a host that does not pass `origin`. What a host may notice:

- `Message`, `inspector.list_messages` rows and the dicts of
  `MessagePort.get_messages` have the key `origin` (`None` unless the host
  started that turn with one). A host with its own `MessagePort` does not
  need to return it.
- `ChatRunner.approval_applies(tool_id)` is new; `kernel.run_tool` uses it.

### Upgrading to 0.8.16

No migration. One change, in `kernel.run_tool` / `run_tool_async` (new in
0.8.15):

- **The approval callback is now asked.** In 0.8.15 `run_tool` refused every
  call the callback would be asked about in a turn, without asking it; with
  `risk_threshold="low"` that was every tool. Now it asks the callback and
  runs the call only when it approves. If your callback can block waiting
  for a person, make it check `ctx.outside_turn` (new field of
  `ToolExecContext`, `True` in a `run_tool` call) and return
  `ApprovalDecision.defer(...)` or `False` there. Tools with
  `requires_approval` are still never run outside a turn.

### Upgrading to 0.8.17

Migration 20 (PostgreSQL 14): the column `sessions.learn`, `1` for every
existing session. Nothing changes for a host that does not pass
`learn=False` to `close_session`. What a host may notice:

- `Session`, the rows of `inspector.list_sessions` and the HTTP session
  response have the key `learn` (`True` unless the session was closed with
  `learn=False`).
- A host that closed sessions through the private
  `kernel._sessions.close(session_id, summary=None)` to skip the learning
  can call `kernel.close_session(session_id, learn=False)` instead; see
  "Closing a session without learning from it".

### Upgrading to 0.8.18

No migration. The changes are in `symbiote.browser`, and none turns itself
on (see "Busca e leitura sem serviço pago" in
`docs/integrations/symbiote-browser-quickstart.md`). What a host that
already registers the web tools may notice:

- **`web_search` output** has the key `outcome` (`"results"` or
  `"no_results"`); an empty answer also has `note`. With a list in
  `search_backend` it has `source` and, when an earlier provider failed,
  `attempts`.
- **`web_search` description** no longer names `browser_navigate` /
  `browser_snapshot` unless the browser tools are registered in the same
  `register()` call; it names `web_extract` when that one is.
- **`web_extract`** also checks against `policy` the address a provider
  reports as the final one (the key `url` of its answer), so a redirect to a
  denied site is refused.
- New names: `extract_backend="http"`, `search_backend="duckduckgo"` /
  `"yahoo"`, `search_options["fetcher"]`, and the `[web]` extra for
  `fetcher="chrome"`.

### Upgrading to 0.8.19

Migration 21 (PostgreSQL 15): the columns `sessions.tool_mode` (`NULL` for
every existing session) and `environment_configs.instant_read_results`
(`0`). Nothing changes for a host that uses neither. What a host may notice:

- `Session`, the rows of `inspector.list_sessions` and the HTTP session
  response have the key `tool_mode` (`None` unless the session chose a mode
  with `set_session_tool_mode`).
- `EnvironmentConfig` and the HTTP config have `instant_read_results`
  (`False`). See "Tool Mode Selection".

### Upgrading to 0.8.21

No migration, and nothing turns itself on. What is new, all of it opt-in:

- **`SymGatewayLLMAdapter(streaming=True)`** reads every reply as an event
  stream and gives the adapter a `stream()`, so `on_token` receives the text
  in the pieces the model wrote; `on_activity=` is called for every line.
  Without the option the adapter still has no `stream` and sends the same
  body. A host that subclassed the adapter to stream (overriding `complete`
  / `stream` and reading the wire itself) can drop that code. See "LLM
  through SymGateway".
- **`provider_options`** on the same adapter (`provider_options=`, the
  attribute, `with_model(..., provider_options=)`) and the public
  `models()`. One thing to check: a `provider_options` key in the
  `llm_config` of a turn, which the adapter used to drop, is now sent to
  the gateway.
- **`KernelConfig.loop_timeout_counts_approval`** (default `True`, the
  behaviour of before). `False` takes the wait for the approval callback off
  the clock of `loop_timeout`. `ChatRunner` and `LongRunRunner` take the same
  keyword. A host that replaced `symbiote.runners.chat.time` with a clock of
  its own to get this can drop that. See "Timeout Configuration".

---

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

`kernel.discover_tools(sym.id, url="http://host:8000")` fetches
`{url}/openapi.json`. When the description lives somewhere else, parse it
yourself and pass the `dict`: the tools are the same and follow the same
approval and loading steps.

```python
spec = yaml.safe_load(Path("vendor/api.yaml").read_text())
result = kernel.discover_tools(
    sym.id, "vendor/api.yaml", spec=spec, base_url="https://api.vendor.example"
)
# base_url defaults to spec["servers"][0]["url"]; the path is only recorded
# as where the description came from.
```

#### Registering the operations yourself (`parse_openapi`, `http_config_for`)

`discover_tools` stores the operations as tools of one symbiote, and
`load_discovered_tools` authorizes them for that symbiote with one credential
for the whole load. That fits a host that plugs its own API into a symbiote.
A host that manages integrations with other people's APIs (kept per workspace
or per customer, switched on symbiote by symbiote, each with its own
credential) needs the two steps alone. Both read and build; neither stores,
registers or authorizes anything.

```python
from symbiote.discovery import http_config_for, parse_openapi
from symbiote.environment.descriptors import ToolDescriptor

errors: list[str] = []
for op in parse_openapi(spec, base_url="https://api.vendor.example", errors=errors):
    kernel.tool_gateway.register_http_tool(
        ToolDescriptor(
            tool_id=f"vendor__{op.tool_id}",       # your id: two descriptions may both have "search"
            name=op.name,
            description=op.description,
            parameters=op.parameters,
            tags=op.tags,
            risk_level=op.risk_level,
        ),
        http_config_for(
            op.method, op.url_template, op.parameters,
            header_factory=lambda: {"Authorization": f"Bearer {vendor_token()}"},
        ),
    )
# Nobody may call them yet: authorize per symbiote, when a person decides.
kernel.environment.add_tools(sym.id, ["vendor__search"])
```

- `parse_openapi(spec, base_url=None, *, errors=None)` returns one
  `OpenAPIOperation` per operation: `tool_id` (a suggested id: a slug of the
  `operationId`, or of method and path), `operation_id`, `name`,
  `description`, `method`, `path` (with the query template), `url_template`
  (`base_url` + `path`), `parameters` (JSON Schema), `tags` and `risk_level`.
  `base_url` defaults to the first entry of `servers`. A malformed operation
  is skipped and, when you pass a list, reported in `errors`; something that
  is not an OpenAPI description raises `ValidationError`.
- `http_config_for(method, url, parameters, *, headers=None,
  header_factory=None, allow_internal=False, timeout=30.0)` returns the
  `HttpToolConfig`: which parameters go in the body, which are optional in
  the query, which are lists. The credential is of that one tool, whatever
  its origin. Leave `allow_internal` off for an API that is not your own: it
  lets the tool reach loopback and private addresses.
- The rules of encoding, origin and network still apply when the tool runs,
  inside the gateway. It is the same conversion `discover_tools(spec=...)`
  stores.
- Tool ids are one namespace per kernel: registering an id again replaces the
  tool for every symbiote. Prefix yours.

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

A session may narrow all of this for itself, whatever the visibility: see
[Narrowing a session's tools](#narrowing-a-sessions-tools-tools_allowed).

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

# Send a message through a model turn
response = kernel.message(session.id, user_message)

# Or append a ready message: no model, tools, trace, score or usage cost
notice = kernel.append_message(
    session.id,
    role="assistant",
    content="Your report is ready.",
    origin="host.report",
    idempotency_key="report:42:ready",
)

# Close (triggers reflection + scoring + failure memory)
closed = kernel.close_session(session.id)
# closed.summary contains the auto-generated session summary
```

`append_message(session_id, role, content, origin=None, idempotency_key=None)`
accepts the `user`, `assistant`, and `system` roles and non-empty text. It writes
straight to the same persisted transcript that `message()` uses, so the result
appears in `kernel.inspector.list_messages()`, later model context, exports, and
session summaries. It does not reopen a closed session and does not run hooks,
reflection, a model, or any tool.

When `idempotency_key` is present, it is scoped to the session and stored
transactionally with the message. Retrying the same normalized payload (role,
content, and origin) returns the original `Message`, including its id and
creation time, even after restart or from another replica. Reusing that key
with a different payload raises
`symbiote.core.exceptions.IdempotencyConflictError`. Hosts should derive stable
keys from their own event IDs; keys must be non-empty strings of at most 500
characters. Omitting the key appends a new message on every call.

What that summary is depends on `session_summary_mode`, per symbiote:

| Mode | The summary |
| --- | --- |
| `"concat"` (default) | the last messages joined; no model call |
| `"llm"` | written once per close by the auxiliary model (`set_evolver_llm`, or the main one when there is none); falls back to the concatenation if the call fails |
| `"off"` | none (`summary` is `None`) |

```python
kernel.environment.configure(symbiote_id=sym.id, session_summary_mode="llm")
```

### Closing a session without learning from it

Some conversations are worth keeping but not worth learning from: a test, a
conversation that went wrong, a subject that must not become memory.

```python
closed = kernel.close_session(session.id, learn=False)
# closed.learn is False, closed.summary is None
```

With `learn=False` the session is only closed. None of the steps of a normal
close runs: no score, no summary, no reflection (so no new memories), no
failure memory, no handoff memory, no skill review, and this close does not
start a dream cycle. No model is called.

The session is marked: `Session.learn` is `False`, and so is `learn` in the
rows of `inspector.list_sessions` and in the HTTP session response (column
`sessions.learn`). Nothing learned for the symbiote as a whole reads a marked
session:

| Reader | What it leaves out |
| --- | --- |
| Harness version scores | the session adds no score to the active versions |
| Parameter tuner, harness evolver | its traces and its score |
| Dream | it does not count towards `dream_min_sessions`; `mine` and `evaluate` do not read its traces, score or messages |
| Skill review | no pass over it, at the close or later |

What stays as it is:

- The messages, the traces, the turn records and the audit rows. The
  retention windows treat it like any other closed session.
- What the symbiote learned while the conversation ran, such as a memory the
  model stored with a tool or a skill review that ran in the middle of it.
- A score the session already had (from an earlier normal close, or from
  `report_feedback`, which can still be called for it). It remains in
  `inspector.list_scores`; the readers above skip it.

The mark belongs to the session and lasts until the session is closed with
learning. A marked session that is reopened (`get_or_create_session` with its
external key) stays marked while it is open, and the next close decides:
`close_session(id, learn=False)` keeps the mark; `close_session(id)` lifts it
and learns from the session as a whole, the earlier messages included. A host
that wants a conversation to stay out for good closes it with `learn=False`
every time, and `Session.learn` says which sessions those are.

Over HTTP it is `POST /sessions/{id}/close?learn=false`, in the SDK
`client.close_session(session_id, learn=False)`, and in the CLI
`symbiote session close <SESSION_ID> --no-learn`.

### Narrowing a session's tools (`tools_allowed`)

The same symbiote often serves different audiences: its owner, signed-in
customers, anonymous visitors. Each may be allowed a different set of tools,
and the host changes those rules without publishing another revision. A
session carries the list of tools it can use:

```python
visitor = kernel.start_session(
    sym.id, subject_key="visitor:9f2", revision="2026-11-a",
    tools_allowed=["search_products", "search_knowledge"],
)
owner = kernel.start_session(sym.id)                  # no list: every authorized tool

kernel.set_session_tools(visitor.id, ["search_products"])   # from the next turn on
kernel.set_session_tools(visitor.id, None)                   # lift the restriction
```

- **It only narrows.** The session sees and may call the tools that are both
  in its list and authorized by the configuration it reads (the revision it
  is pinned to, or the current one). A tool in the list that the
  configuration does not authorize stays refused: the list is not a way
  around the policy.
- **Out of the list, out of the prompt.** Such a tool is not shown to the
  model, under either `tool_visibility`, and a call to it is refused by the
  policy gate and written to the audit log as `blocked`, like any
  unauthorized call. The model reads `Tool 'x' blocked: Tool 'x' is not
  available in this conversation`.
- **`None` is no restriction; `[]` is no tool at all.**
- **It covers the tools a mode adds by itself.** `get_tool_schema`
  (`tool_loading="index"`) and `search_memories` / `search_knowledge`
  (`context_mode="on_demand"`) are normally allowed by their mode without
  being in `tools`. In a narrowed session they must be in the list too, and
  the prompt stops telling the model to use the ones that are not.
  `kernel.environment.mode_tools(sym.id)` returns the ones the symbiote's
  modes add, so you can include them on purpose:

  ```python
  allowed = ["search_products", *kernel.environment.mode_tools(sym.id)]
  ```

  Without `search_knowledge`, a session of an `on_demand` symbiote has no way
  to reach the knowledge (nothing is injected in its place, since leaving it
  out may be the point). Without `get_tool_schema`, a session of an `index`
  symbiote is shown the full schemas of its tools instead of the index, so
  they stay usable.
- **It follows the delegation.** The session of a symbiote this one delegates
  to (`spawn`) carries the same list, so the list bounds what the
  conversation can make happen anywhere in the chain, in the intersection
  with what each delegate is authorized to do. A tool id means the same for
  every symbiote: to let a delegate use one of its own tools for this
  audience, put it in the list. `spawn` itself must be in the list for the
  delegation to happen.
- **A call that waited for a person** (`resolve_pending`) is checked against
  the list the session has when it finally runs.
- **On record.** `Session.tools_allowed`, the `tools_allowed` of each row of
  `inspector.list_sessions`, and the `blocked` rows of the audit log.
- `get_or_create_session(..., tools_allowed=[...])` gives the list to a
  session it creates; one that already exists keeps what it has.
- Over HTTP: `tools_allowed` on `POST /sessions`, and `PUT
  /sessions/{id}/tools` with `{"tools_allowed": [...]}` or `null`.

A session has one more setting of the same kind, `delegation`: see
[Revisions](#revisions-changing-a-symbiote-while-it-serves-people).

### End users: one memory partition per person (`subject_key`)

Use this when one symbiote talks to several people, for example a symbiote
published on a site or behind your own app. Without it, memory belongs to the
symbiote as a whole: what one person tells it is recalled in everybody's
conversations.

Name the person when you start the session:

```python
session = kernel.start_session(sym.id, subject_key=f"customer:{customer.id}")
# or, with an external key:
session = kernel.get_or_create_session(
    sym.id, external_key=f"chat:{chat.id}", subject_key=f"customer:{customer.id}"
)
```

`subject_key` is an opaque string you choose (a user id from your app, a
visitor cookie). The kernel only compares it for equality; it must not be
empty and takes up to 500 characters. It is set when the session starts and
cannot change: `get_or_create_session` raises `ValidationError` when the
session already exists under a different `subject_key` (or under none), so one
person's conversation never continues as another's.

Two rules follow from it:

| | A session with `subject_key="S"` | A session without `subject_key` |
|---|---|---|
| **Reads** | the shared memories plus the ones of `S` | the shared memories only |
| **Writes** | only to the partition of `S` | the shared memories |

So what you teach the symbiote in your own sessions (no `subject_key`) is
shared and reaches every end user, read-only for them. What an end user says
is learned for that end user alone: reflection, failure memories, the long-run
handoff, `capabilities.learn` and `search_memories` all stay inside the
partition, and a PATCH from LLM reflection cannot reach a shared memory. No
call site needs to pass the key: `MemoryStore` takes it from the session.

What else to know:

- **Delegation.** A symbiote reached through `spawn` works for the same end
  user as the session that delegated, in its own memory: the partition is the
  pair (symbiote, `subject_key`).
- **Knowledge and skills are not partitioned.** Knowledge is what you register
  for the symbiote; every session reads it.
- **Learning from end users' sessions is off by default.** Those sessions get
  a score and their own memories, but they do not move the harness (parameter
  tuner, evolver, per-version scores), the dream's `mine` and `evaluate`
  phases or the skill review. Turn it on per symbiote with
  `kernel.environment.configure(symbiote_id=sym.id, learn_from_subject_sessions=True)`.
  With it on, a skill or a shared insight can be written from what an end user
  did or said; leave it off unless you trust the people talking to the
  symbiote.
- **Dream.** `prune` judges each memory alone and covers every partition.
  `reconcile` and `generalize` work on the shared memories only, so nothing is
  merged across end users.
- **`external_key` and `host_context` are different things.** `external_key`
  finds a conversation again; `host_context` carries the caller's data to your
  tools for one turn. Neither separates memory.

Inspecting and erasing:

```python
kernel.inspector.list_subjects(sym.id)
# [{"subject_key": "customer:42", "sessions": 3, "last_session_at": "...", "memories": 5}, ...]
kernel.inspector.list_memories(sym.id, subject_key="customer:42")  # that person's
kernel.inspector.list_memories(sym.id, subject_key=None)           # the shared ones
kernel.inspector.list_sessions(sym.id, subject_key="customer:42")

# Everything the kernel stored about one person under this symbiote: sessions,
# messages, traces, scores, audit rows and memories. One transaction; cannot
# be undone. dry_run=True returns the counts without deleting.
kernel.purge_subject(sym.id, "customer:42")
```

`purge_subject` covers one symbiote. If the person also reached other
symbiotes through `spawn`, call it for each of them.

Over HTTP, `POST /sessions` accepts `subject_key`, `GET /memory/search` accepts
it as a query parameter, and `PUT /symbiotes/{id}/config` accepts
`learn_from_subject_sessions`; `SymbioteClient.create_session(...,
subject_key=...)` is the SDK equivalent.

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
    # list_messages returns the last n messages in chronological order.
    return [
        {"role": m["role"], "content": m["content"], "origin": m["origin"]}
        for m in kernel.inspector.list_messages(session_id, limit=n)
        if m["role"] in ("user", "assistant")
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
- A user turn may carry `origin` (what `list_messages` returns for it): the
  model is then told that the user did not write that text. See
  [Turns the host starts](#turns-the-host-starts).

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

Three settings decide how much of the tool results the model reads; the
first two have to be raised together:

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
- `loop_compaction_rounds` (default `4`): independently of the budget,
  once a turn has made this many tool rounds, every round replaces the
  older exchanges of the loop with one line per step (`1) read_file → ...`);
  only the latest exchange stays in full. `None` or `0` turns this off, so
  the older results stay in full until the 80% of `loop_context_budget`
  above. A host whose model reads files over many rounds (a coding agent)
  turns it off and raises `loop_context_budget`: with the default, the
  model loses what it read four rounds ago and reads it again.

A single result should fit in half of that 80%, so keep
`loop_context_budget >= tool_result_max_chars * 0.625` at the very least (the
kernel logs a warning at startup when it is not). That minimum only holds two
full results and nothing else, so leave room for the system prompt, the
history and the earlier steps: with `tool_result_max_chars=30000` the minimum
is 18,750 tokens, and something like 48,000 to 64,000 is a realistic pairing.
The same ceiling, and `loop_compaction_rounds`, apply inside the blocks of
`tool_mode="long_run"`.

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

### What a turn returns, and why it stopped

`message()` and `message_async()` return the reply text (`str`) for a turn
that called no tool, and a `TurnReply` dict for one that did:

```python
from symbiote.core.turn_result import TurnReply

reply = kernel.message(session.id, "Rotate the key")
if isinstance(reply, dict):          # TurnReply
    text, results = reply["text"], reply["tool_results"]   # results: list of dicts
else:
    text, results = reply, []
```

`turn()` and `turn_async()` take the same arguments and always return a
`TurnOutcome`, which also says why the tool loop stopped, so the host does
not have to read `execution_traces`:

```python
outcome = kernel.turn(session.id, "Rotate the key", host_context={"user": uid})
outcome.text            # the reply
outcome.tool_results    # list[ToolCallResult], every call the loop made
outcome.stop_reason     # see the table below
outcome.stopped_early   # True when a limit ended the loop, not the model
outcome.iterations, outcome.tool_calls, outcome.elapsed_ms, outcome.tool_mode

outcome = await kernel.turn_async(session.id, text, on_token=send)
```

| `stop_reason` | The loop ended because |
| --- | --- |
| `end_turn` | the model answered without asking for more tools (also a turn with no tool at all) |
| `max_iterations` | the turn reached `max_tool_iterations` |
| `stagnation` | the same successful call was repeated |
| `circuit_breaker` | a tool failed three times in a row |
| `timeout` | the loop ran past its time limit |
| `budget` | the turn reached its spending ceiling (`max_turn_tokens` / `max_turn_llm_calls`) |
| `on_after_tool_result` | the host's `on_after_tool_result` hook ended the turn |
| `block_failure` | a `long_run` project ended with blocks not completed |

For `max_iterations`, `stagnation` and `circuit_breaker` the text is the
forced final answer described above; for `on_after_tool_result`, what the
hook returned; for `timeout` and `budget`, a fixed notice; for `block_failure`, the reply composed from the
blocks that completed. The same reason is
stored in `execution_traces.stop_reason`. A turn that does not return raises
as `message()` does (`TurnCancelledError`, `SessionLockLostError`,
`CapabilityError` when the run failed).

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
does not carry it. `SymGatewayLLMAdapter` streams when built with
`streaming=True`, text and native tool calls alike (see "LLM through
SymGateway"); without it, it makes one complete call. Use the progress
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

Each call replaces all the callbacks (omitted ones are cleared). They are
shared by every session of the kernel and receive no session id; in
`message_async` they may run on a worker thread. For per-tool detail (tool id,
params, result) use the `before_tool` / `after_tool` hooks.

### Visible reasoning

A model that reasons spends most of a turn thinking without writing a word.
With two opt-ins the host receives that reasoning while each model call runs:
an adapter built with `stream_reasoning=True` and the `on_reasoning`
callback.

```python
from symbiote.adapters.llm.symgateway import SymGatewayLLMAdapter

llm = SymGatewayLLMAdapter(api_key, model=model, streaming=True, stream_reasoning=True)
kernel = SymbioteKernel(config, llm=llm)

def on_reasoning(text: str, iteration: int) -> None:
    # A piece of the reasoning of model call ``iteration``; group by it.
    show_thinking(iteration, text)

kernel.set_progress_callbacks(on_progress=on_progress, on_reasoning=on_reasoning)
```

- `on_reasoning(text, iteration)` receives the pieces as the provider sends
  them, before the text they lead to, in every model call of the turn: each
  round of the tool loop, chat and long-run blocks alike. `iteration` is the
  number `on_progress` gives the call's `iteration_start`; the answer forced
  by a step limit and instant mode's reading of the results get the next
  number, a retried call keeps its own, and in a long run the count starts
  again in each block (`block_start` tells which). The long run's planner,
  evaluator and final report are not streamed.
- With the callback set, model calls go through the adapter's `stream()`
  even when the turn has no `on_token`.
- The reasoning is never part of the answer: it does not enter the text the
  turn returns, `on_token`, `on_stream`, the history, the memory, the
  reflection or what a delegated symbiote returns. The turn's text, history
  and memory are the same with and without it. `LLMResponse.reasoning_state`,
  which some protocols send back to the provider, is untouched.
- `LLMTokenUsage.reasoning_tokens` is filled from a streamed call's usage
  when the provider reports it (`completion_tokens_details` or
  `output_tokens_details`).
- Without `stream_reasoning=True` an adapter's stream yields what it always
  did, and the request body is the same with or without it. Without the
  callback the kernel calls the adapter as before. An adapter of your own
  can take part by yielding `symbiote.environment.descriptors.ReasoningDelta`
  items from `stream()`; the kernel never mixes them into the text.

What each adapter reads:

| Adapter | Where the reasoning comes from |
|---------|--------------------------------|
| `SymGatewayLLMAdapter(streaming=True, stream_reasoning=True)` | `delta.reasoning_content` (or, without it, `delta.reasoning`) in the gateway's event stream. `stream_reasoning` without `streaming` is an error. |
| `ForgeLLMAdapter(stream_reasoning=True)` | forge_llm's stream chunks (0.7.20 and later): Anthropic's thinking, OpenAI's reasoning summary, and `delta.reasoning_content` / `delta.reasoning` of OpenAI-compatible providers (DeepSeek, vLLM, xAI, OpenRouter, Groq). When `stream()` falls back to one complete call (native tools without `stream_tool_calls`), the reply's whole `reasoning_content` comes once, before its text. |

**Asking for visible reasoning.** Whether the model shows its reasoning at
all is asked of the provider, in its own terms, through `provider_options`.
Both adapters take it the same way: at construction or on the attribute for
every call, and in the config of one call (the `llm_config=` of a turn)
merged over it, key by key. Which model takes which option is the host's
decision:

```python
from symbiote.adapters.llm.forge import ForgeLLMAdapter

ANTHROPIC = {"thinking": {"type": "enabled", "budget_tokens": 4000}}
OPENAI = {"reasoning": {"summary": "auto"}}

llm = ForgeLLMAdapter(provider="anthropic", model=model, stream_reasoning=True,
                      provider_options=ANTHROPIC)
kernel.message(session.id, text, llm_config={"provider_options": OPENAI})  # one turn
```

- `ForgeLLMAdapter` sends them as forge_llm's `ChatConfig.extra` (0.7.20 and
  later). Anthropic: `max_tokens` must be above `budget_tokens`; forge_llm
  raises it when the call does not set one. The thinking blocks come back on
  the reply that called tools and the kernel sends them back in the next
  round, as Anthropic requires; with `stream_tool_calls=True` the stream
  carries them too. OpenAI: a call that asks for a summary goes through the
  Responses API, the only one that returns it.
- `SymGatewayLLMAdapter` sends them in the body as `provider_options`
  (SymGateway 0.34.164 and later). The gateway relays the provider's
  reasoning (Anthropic's `thinking_delta`, OpenAI's reasoning summary, which
  it gets from the Responses API) as `delta.reasoning_content`. A reply that
  called tools carries the gateway's opaque replay state, `reasoning_state`
  (in the terminal chunk of a stream, or in the whole message); the adapter
  returns it on `LLMResponse.reasoning_state` and sends it back unchanged on
  the assistant message that called the tools, as the gateway requires.
  It stays in the tool loop, never in the history or the memory.
  `models()` says which provider serves each model.

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

The final score combines auto-score (60%) with user feedback (40%). The
feedback may come before or after `close_session`: either way it stays in the
final score. A turn that called no tool gives the loop nothing to measure, so
its auto-score is the neutral 0.8; the host's own score is what tells such
sessions apart.

---

## Human-in-the-Loop

Tools can declare a `risk_level` (`"low"`, `"medium"`, `"high"`). The host
provides an approval callback. By default the gate fires **only for
`risk_level == "high"`** tools — low/medium execute and the callback is not
consulted for them.

`risk_threshold` sets the lowest risk that goes to the callback:

| `risk_threshold` | The callback is asked for |
| --- | --- |
| `"high"` (default) | high-risk tools |
| `"medium"` | medium- and high-risk tools |
| `"low"` | every tool call |

```python
kernel.set_approval_callback(approval_gate, risk_threshold="medium")
```

Any other value raises `ValueError`. The threshold applies to the whole
kernel (chat turns, `instant` mode and the blocks of `tool_mode="long_run"`),
not per symbiote or per user; a host that wants it to vary per user installs
the lowest threshold it needs and lets the callback answer `True` by itself
for the rest (`pass_context=True` gives it the turn's `host_context`). Calling
`set_approval_callback` again replaces both the callback and the threshold.

A tool whose descriptor sets `requires_approval=True` runs only with a
person's yes: the callback is asked whatever its risk level, and with **no**
callback installed the call is refused (the model gets an error saying so)
instead of running unchecked. The browser tools attached to the person's own
Chrome (`symbiote.browser.register_user_browser`) set it on clicking and
typing. A host that turns its approvals "off" for everything else keeps the
callback installed and returns `True` for tools where
`kernel.tool_gateway.requires_approval(tool_id)` is false. The rule applies to
calls the model makes in the chat and long-run loops; a host calling
`tool_gateway.execute()` itself decides on its own.

**Embedded hosts (the kernel owns the runner) — use `set_approval_callback`:**

```python
def approval_gate(tool_id: str, params: dict, risk: str) -> bool:
    # Called for tools at or above risk_threshold (default: high) and for
    # tools with requires_approval. Return True to allow, False to deny.
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
    approval_risk_threshold="high",  # or "medium" / "low"
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

### Deferring the decision: approvals that wait

The callback can only block the turn while it waits for a person. When nobody
can answer now (the person who decides is not the one in the conversation),
answer `ApprovalDecision.defer()` instead of `True` / `False`:

```python
from symbiote.environment.policies import ApprovalDecision

def approval_gate(tool_id, params, risk, ctx):
    if ctx.host.get("is_owner"):
        return True
    return ApprovalDecision.defer("asked by a visitor", expires_in=48 * 3600)

kernel.set_approval_callback(approval_gate, pass_context=True)
```

The call is **not executed**. The kernel stores it as a pending approval, the
model receives a tool result saying that the action waits for a person (your
own text with `defer(message=...)`), and the turn ends normally.
`ApprovalDecision.approve()` and `ApprovalDecision.deny("reason")` are the
other two answers; `deny` with a reason gives that reason to the model.

```python
outcome = kernel.turn(session.id, "cancel my order")
outcome.pending_approvals            # ids of the calls this turn left waiting
kernel.inspector.list_pending(sym.id)             # the ones still waiting
kernel.inspector.list_pending(sym.id, status=None, subject_key="customer:42")
kernel.inspector.get_pending(approval_id)

resolved = kernel.resolve_pending(
    approval_id, True, note="ok", resolved_by="owner-1", host_context={...}
)
resolved.status   # "executed" (resolved.result) or "failed" (resolved.error)
kernel.resolve_pending(other_id, False, note="not this month")   # "denied"
```

- **What runs.** An approved call runs inside `resolve_pending`, with the
  symbiote, session, end user and parameters it was asked with. Policy and the
  tool guard are checked again, since permissions may have changed meanwhile.
  The guard and the handler see `current_tool_context().approval_id`, so a
  guard that refuses a tool to visitors lets the approved call through, and
  the audit row of the execution carries the same id. The `host_context` of
  the turn that asked is not kept: pass the one the execution needs.
- **Once.** A request is resolved once; a second `resolve_pending`, or one for
  an expired request, raises `ValidationError`. Two people deciding at the
  same moment run the call once.
- **Expiry.** A request expires after `KernelConfig.pending_approval_ttl_hours`
  (default 168; `None` never) or after `defer(expires_in=seconds)`.
  `kernel.expire_pending()` closes the ones past their time; it also runs at
  the start of every turn and inside `resolve_pending`.
- **Restart.** Requests are rows in `pending_approvals`: they are there after a
  restart, as long as the tool is registered again.
- **What the conversation hears.** At the start of the next turn of that
  conversation, or of that end user (`subject_key`) with the same symbiote in
  any session, the model finds in its context what became of each request
  (executed and the result, failed, denied with the note, or expired), once.
  A request made by a delegated symbiote (`spawn`) is told to the
  conversation that delegated.
- **Parameters.** While a request is pending its parameters are stored as they
  came, because running the call needs them; they are erased when it is
  resolved or expires. What `list_pending` shows (`params`) has the
  `secret_params` and every secret the redactor knows removed.
- **Erasure.** `purge_subject` and `purge_symbiote` remove the requests too.
- Over HTTP: `GET /symbiotes/{id}/approvals?status=pending` and
  `POST /approvals/{id}/resolve` with `{"approved": true, "note": "..."}`;
  `SymbioteClient.list_approvals` / `resolve_approval`.

### Host veto: the tool guard

The approval callback asks a *person*, and only about the risks the host
selected (high, by default). The tool
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
where `host` is a read-only view of the mapping (`approval_id` is set as well
when the call is the execution of an approval that waited, see above):

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

### Delegation: knowing that the code runs for a delegated symbiote

When a symbiote calls `spawn`, the delegated symbiote's turn runs inside the
turn of the one that delegated: your LLM adapter, hooks, tool guard and tool
handlers fire for it too. `current_spawn()` tells them apart:

```python
from symbiote.environment.exec_context import current_spawn

spawn = current_spawn()      # None outside a delegation
if spawn is not None:
    spawn.symbiote_id, spawn.session_id                 # the delegated symbiote and its sub-session
    spawn.parent_symbiote_id, spawn.parent_session_id   # who called spawn
    spawn.root_symbiote_id, spawn.root_session_id       # the turn your host started
    spawn.depth                                         # 1, or 2 when the delegate delegates again
```

---

## Turns the host starts

A turn usually answers something a person wrote. A host may also start one by
itself: a job the symbiote was waiting for finished, a webhook arrived, a
scheduled check came due. Two primitives cover it. The scheduling, the queue
of events and the waiting belong to the host: the kernel starts no thread and
no timer for a symbiote that is not in a turn.

### `origin`: an input nobody typed

```python
kernel.message(
    session_id,
    "The export you were waiting for finished: 3 files, no errors.",
    origin="wake",
)
```

`origin` is a label the host chooses (`"wake"`, `"webhook"`, `"schedule"`):
1 to 64 letters, digits, `_`, `.`, `:` or `-`. `message`, `turn`,
`message_async` and `turn_async` take it; `None` (the default) is the person.
Anything else raises `ValidationError` before the turn starts. The turn is an
ordinary one: same lock, same hooks, same streaming, same `cancel_turn`.

What the label changes:

| Where | With `origin="wake"` |
|---|---|
| Stored message | `role` stays `"user"` (providers alternate user and assistant); `origin` is `"wake"`. `Message.origin`, `inspector.list_messages`, `MessagePort.get_messages` and the markdown export return it, so a host draws the message as an event, not as something the person said |
| What the model reads | The text under the line `[Not written by the user. Source: wake]`. The stored content is the text as the host wrote it |
| Later turns | A user turn of `conversation_history` that carries `"origin"` gets the same line. Forward the key as `list_messages` returns it |
| `before_turn` hook | The message dict has `"origin": "wake"` |
| Learning | The keyword reflection reads only what the person wrote; the transcripts given to the reflection, skill review and dream models name the message `[wake]` instead of `[user]` |

Over HTTP, `POST /sessions/{id}/chat` and `/chat/stream` take `origin` in the
body and the message listings return it; `SymbioteClient.chat(...,
origin=...)` sends it.

### `run_tool`: one tool call as the session, outside a turn

```python
result = kernel.run_tool(
    session_id, "job_status", {"job": "exec-2ad2"}, host_context={"user": user_id}
)
if result.success and result.output["status"] in ("succeeded", "failed"):
    kernel.message(session_id, f"The job ended: {result.output}", origin="wake")
```

`run_tool` (and `run_tool_async`, which awaits coroutine handlers) runs one
call with the rules of that conversation and calls no model:

- **Same checks as in a turn.** The session's revision, the tools it is
  narrowed to (`tools_allowed`), policy, the tool guard, the tool hooks and
  the audit log. Calling `kernel.tool_gateway.execute` yourself skips the
  first two: it reads the current configuration and ignores `tools_allowed`.
- **`host_context`** is bound for the call, as in `message()`. The context of
  earlier turns is not kept: pass what the tool needs (a credential, the
  caller) again.
- **Nothing is added to the conversation.** No message, no trace. What to do
  with the result is the host's decision.
- **Approval: your callback is asked, a person is not.** The approval
  callback is asked about the calls it is asked about in a turn (risk at or
  above its threshold), after policy and the guard. It is your rule and often
  answers without asking anybody. For it, for the guard and for the handler,
  `ToolExecContext.outside_turn` is `True`: nobody is following a turn, so a
  callback that would wait for a person must answer at once instead.

  | The callback answers | The call |
  |---|---|
  | `True` / `ApprovalDecision.approve()` | runs |
  | `False` / `ApprovalDecision.deny(reason)` | is refused; `audit_log` has `denied:<reason>` |
  | `ApprovalDecision.defer(reason)` | is refused (`blocked:needs approval`); no pending approval is recorded outside a turn |
  | raises | is refused |

  A tool whose descriptor sets `requires_approval` is never run by
  `run_tool`, and the callback is not asked: only a person's yes runs it, in
  a turn or through `resolve_pending`. The callback runs on the calling
  thread (a worker thread in `run_tool_async`).

  ```python
  def approval_gate(tool_id, params, risk, ctx):
      if my_rules_allow(tool_id, ctx):
          return True
      if ctx.outside_turn:              # nobody to ask: do not wait
          return ApprovalDecision.defer("needs the owner")
      return ask_the_owner_and_wait(tool_id, params)

  kernel.set_approval_callback(approval_gate, pass_context=True, risk_threshold="low")
  ```
- **Result, not exception.** A refused, unknown or failing call comes back as
  `ToolResult(success=False, error=...)`. Only an unknown session raises
  (`EntityNotFoundError`).
- **No session lock.** The call may run while a turn of the session runs.
  `timeout` defaults to the symbiote's `tool_call_timeout`.

### Building "wait and wake" on top

The kernel has no wait: a host builds it with a tool of its own, its own
table and its own schedule.

1. Register a `wait` tool (`register_descriptor(..., pass_context=True)`).
   Its handler stores what the symbiote waits for (an event name, a tool to
   ask with a condition, or a time), the deadline and the session, and
   answers at once; the turn ends normally.
2. From your schedule, ask the tools that are being waited on with
   `run_tool`. A model call is spent only when something changed.
3. When the event arrives, the condition holds or the deadline passes, start
   the turn with `origin="wake"` and a note saying what was awaited, what
   happened and what the symbiote meant to do next. If the session is busy
   the turn waits for the lock (`session_lock_wait`) like any other.
4. Tell the model in the persona that, when something keeps running, it
   leaves a wait and ends the turn, instead of asking to be called back.

---

## Spending ceiling of a turn

`max_tool_iterations` limits the rounds of a turn, not what they cost: a turn
with large documents or long tool results can cost many times the usual in a
few rounds. Two per-symbiote ceilings, both off (`0`) by default:

```python
kernel.environment.configure(
    symbiote_id=sym.id,
    max_turn_llm_calls=8,      # always works
    max_turn_tokens=60_000,    # needs an LLM adapter that reports usage
)

outcome = kernel.turn(session.id, text)
if outcome.stop_reason == "budget":
    ...   # outcome.text is a fixed notice; outcome.usage says what was spent
```

- **What is counted:** every model call made for the turn so far, the ones of
  a symbiote it delegated to (`spawn`) included, and the tokens their adapters
  reported (see below). With an adapter that reports no usage,
  `max_turn_tokens` has nothing to count; `max_turn_llm_calls` does not depend
  on it.
- **When it is checked:** before each round of the tool loop. The first call
  always runs, and a single call can take the turn past the ceiling: the
  ceiling stops the next call, it does not cut one short.
- **What stops:** no further model call starts, including the extra call that
  writes an answer when `max_tool_iterations` is reached. Tool calls the model
  had already asked for in the last round still run. The turn returns a fixed
  text and `stop_reason="budget"`; the host decides what to show.
- **`long_run`:** the ceiling covers the whole project. No further block
  starts, the evaluator is skipped and the reply is the status of the blocks,
  not a composed answer.
- It is per turn, not per session or per day: a host that needs those adds
  them up from `inspector.usage_totals`.

---

## Revisions: changing a symbiote while it serves people

A change to the persona, the tools or the config applies at once to every
conversation, including the ones in the middle. A revision is a named,
immutable picture of what is configured; a session pinned to it keeps reading
it, whatever is edited afterwards.

```python
# 1. Configure as usual, then take the picture.
kernel.update_symbiote(sym.id, persona={...})
kernel.environment.configure(symbiote_id=sym.id, tools=[...])
kernel.knowledge.register_source(sym.id, "policy", content=text)
kernel.create_revision(sym.id, "2026-11-a", note="refund policy reviewed")

# 2. Serve people on it. Keep editing: they do not see it.
session = kernel.start_session(sym.id, subject_key="customer:42", revision="2026-11-a")
draft = kernel.start_session(sym.id)          # no revision: the current configuration

# 3. Later.
kernel.list_revisions(sym.id)                 # newest first
kernel.inspector.list_revisions(sym.id)       # with the sessions pinned to each
kernel.set_session_revision(session.id, "2026-11-b")   # or None: back to current
kernel.retire_revision(sym.id, "2026-11-a")   # no new session; the pinned ones go on
```

**What a revision holds:** the name, role and persona; the whole
`EnvironmentConfig`, with its list of tools; the knowledge entries, copied
(removing an entry later does not take it out of the revision).

**What it does not hold**, and every session shares: memories, skills, end
users (`subject_key`), the harness texts, and the definitions of the tools. A
revision fixes *which* tools a session may call, not what an HTTP tool's URL
is: to version a tool's definition, register the new one under another id.
What happens after a session closes (summary, reflection, dream, skill
review) follows the current configuration.

**Nothing gets around the policy.** A revision is created from what the
ordinary calls already validated and stored; there is no per-turn override. In
a pinned session a tool the revision does not list is refused even if the
current config allows it, and a tool it lists runs even if the current config
no longer does.

**Rules worth knowing:**

- A label is never reused: `create_revision` with a label the symbiote already
  has raises `DuplicateEntityError`.
- `start_session(..., revision=label)` raises `EntityNotFoundError` for an
  unknown label and `ValidationError` for a retired one.
- `get_or_create_session(..., revision=label)` gives the revision to a session
  it creates. A session that already exists keeps the one it has
  (`Session.revision`): a conversation in progress does not change
  configuration because a newer revision exists. Move it with
  `set_session_revision`.
- **Delegation.** A pinned session that calls `spawn` delegates to the revision
  with the same label of the other symbiote, when it has one that is not
  retired; otherwise to that symbiote's default revision; otherwise the
  delegate reads its current configuration. See the next section.
- **On record.** `Session.revision`, `inspector.list_sessions(revision=...)`,
  the `revision` of each row of `list_turn_contexts` and `list_llm_calls`, and
  `inspector.usage_by("revision", sym.id)` to compare two revisions.
- Inside a turn, `symbiote.core.revisions.current_revision()` returns the
  revision in force. `kernel.environment.get_config()` returns the revision's
  config there; `current_config()` always returns what is stored now, and
  `configure()` always edits that.
- `purge_symbiote` removes the revisions. Over HTTP: `POST` / `GET
  /symbiotes/{id}/revisions`, `GET .../revisions/{label}`, `POST
  .../revisions/{label}/retire`, `revision` on `POST /sessions`, and `PUT
  /sessions/{id}/revision`.

### Delegation between symbiotes published at different times

The members of a team are rarely published together, and each usually has its
own numbering. With labels alone, a session pinned to `boss-v3` that delegates
to a colleague finds no `boss-v3` there and gets the colleague's current
configuration: the person on the other side sees, through the colleague,
whatever its owner is editing.

```python
kernel.create_revision(worker.id, "worker-v7")
kernel.set_default_revision(worker.id, "worker-v7")   # None: no default
kernel.get_default_revision(worker.id)                # the Revision, or None

public = kernel.start_session(boss.id, revision="boss-v3", delegation="strict")
kernel.set_session_delegation(public.id, "open")      # from the next turn on
```

Where a delegation lands:

| The calling session | The delegate answers with |
|---|---|
| pinned to `L` | its revision `L`, if it has one that is not retired; else its default revision; else its current configuration |
| not pinned | its current configuration |
| `delegation="strict"`, pinned to `L` | its revision `L`; else its default revision; else the delegation is **refused** |
| `delegation="strict"`, not pinned | its default revision; else the delegation is **refused** |

- **The default is only where a delegation lands.** `start_session` without
  `revision` still reads the current configuration: that is how an owner
  tries a draft. A host that wants the published version asks for it by
  label.
- **One default per symbiote.** Setting another moves it. It must be a
  revision that takes sessions (not retired), and it cannot be retired while
  it is the default: move the default first.
- **`delegation` is the session's**, not the kernel's: the same symbiote
  serves its owner, for whom reaching a colleague's draft is expected, and
  the public, for whom it is not. It follows the delegation: the sub-session
  is strict too.
- **A refusal is told and recorded.** `spawn` fails with `delegation to
  'worker' refused: it has no revision 'boss-v3' and no default revision, and
  this conversation only delegates to a published revision`, which the model
  reads as the tool's error; no sub-session is created, and the audit row of
  the call has `result = 'error:DelegationRefusedError'`.
- **On record.** The sub-session's `revision` says which one answered;
  `is_default` is on each revision and on the rows of
  `inspector.list_revisions`; `Session.delegation` and the `delegation` of
  `inspector.list_sessions`.
- Labels that are short and shared by accident (`v3` on two symbiotes) match:
  either number per symbiote (`boss-v3`) or share a label on purpose.
- Over HTTP: `PUT /symbiotes/{id}/default-revision` with `{"label": "..."}`
  or `null`, `delegation` on `POST /sessions`, and `PUT
  /sessions/{id}/delegation`.

---

## Model usage: who each call is for and what it cost

Every model the kernel calls is metered, whatever the provider. Inside
`complete()` / `stream()`, an LLM adapter can ask who the call is for:

```python
from symbiote.core.llm_usage import current_llm_call, report_usage

call = current_llm_call()   # None when the adapter is not called by a kernel
call.symbiote_id, call.session_id, call.root_session_id, call.purpose
```

`purpose` is `turn`, `subagent` (a turn of a delegated symbiote),
`reflection`, `session_summary`, `skill_review`, `dream:<phase>`, `harness`,
`memory_consolidation`, `tool_resolution`, `long_run:plan`,
`long_run:evaluate` or `long_run:report`. `root_session_id` is the session of
the turn the host started: it differs from `session_id` for a delegated
symbiote, so the whole cost of a conversation can be added up.

The kernel stores one row per call in `llm_usage`: symbiote, session,
purpose, provider, model, tokens, duration and whether it failed. No text of
the conversation, and no price: the kernel records tokens, the host applies
its prices. The tokens are the `usage` of the `LLMResponse` the adapter
returned. An adapter that returns plain text, or that streams, reports them
before it returns:

```python
from symbiote.core.llm_usage import report_usage, usage_from_provider

class MyAdapter:
    def complete(self, messages, config=None, tools=None) -> str:
        data = call_my_provider(messages)
        report_usage(usage=usage_from_provider(data.usage), provider="acme", model=data.model)
        return data.text
```

Without either, the call is recorded with no token counts. The bundled
`ForgeLLMAdapter` and `SymGatewayLLMAdapter` report both ways.

**Token convention.** Providers count the prompt cache differently, and the
cache changes the price a lot, so every record follows one rule:

| Counter | Meaning |
| --- | --- |
| `prompt_tokens` | the whole input, cache included |
| `cache_read_tokens` | the part of the input read from the provider's cache |
| `cache_write_tokens` | the part of the input written to the cache |
| `cache_write_5m_tokens`, `cache_write_1h_tokens` | the written part, split by how long the provider keeps it (when it says so) |
| `completion_tokens` | the output |
| `reasoning_tokens` | information only: already inside `completion_tokens` |

The input priced at the ordinary rate is `prompt_tokens - cache_read_tokens -
cache_write_tokens`. `None` means the adapter did not report that counter
(the sums treat it as 0). `usage_from_provider(raw)` turns what a provider
returned into this convention: OpenAI's `prompt_tokens` already includes the
cache (`prompt_tokens_details.cached_tokens`); Anthropic's `input_tokens`
does not, so `cache_read_input_tokens` and `cache_creation_input_tokens` are
added to it. An adapter may also pass the counters one by one:
`report_usage(1200, 80, cache_read_tokens=1024)`.

Both bundled adapters fill these counters. `ForgeLLMAdapter` reads the
provider's own usage, which `forge_llm` hands over from version 0.7.17 (this
release pins 0.7.18): for OpenAI and Anthropic, in plain and tool answers
and in streams (it asks the stream for its usage). `SymGatewayLLMAdapter`
reads the gateway's usage.

```python
outcome = kernel.turn(session.id, text)
outcome.usage        # llm_calls, prompt_tokens, completion_tokens, total_tokens,
                     # cache_read_tokens, cache_write_tokens of this turn,
                     # what it delegated included

ins = kernel.inspector
ins.usage_totals(sym.id, since="2026-11-01T00:00:00+00:00")
ins.usage_by("purpose", sym.id)             # also: symbiote, session, root_session,
ins.usage_by("subject", sym.id)             # provider, model, day, subject (end user)
ins.usage_totals(sym.id, purpose="dream")   # a family: every dream:<phase>
ins.usage_totals(root_session_id=session.id)  # a conversation and what it delegated
ins.list_llm_calls(sym.id, limit=50)        # the rows, newest first

kernel.set_usage_callback(lambda record: my_billing.add(record))  # every call, as it ends
```

Every turn, with or without tools, is also on record in
`inspector.list_turn_contexts` with its own totals. `kernel.prune(usage_days=N)`
or `KernelConfig.usage_retention_days` bound the table; `purge_symbiote`
removes a symbiote's rows; `purge_subject` keeps the counts (they name a
session id that no longer exists, nothing else).

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

### Instant mode: a command or a query

An instant turn makes one model call and runs the actions it asks for. When
they all succeed, the turn ends there: the reply is the text of that call,
written before any result existed, and the model is called again only when
an action fails. That is right for a command ("open the editor") and wrong
for a query, where the reply would be "let me look that up" and nothing
else.

```python
kernel.environment.configure(
    symbiote_id=sym.id, tool_mode="instant", instant_read_results=True  # default False
)
```

With `instant_read_results=True`, after the actions succeed the model is
called once more, without tools, to read their results and answer. The turn
is still an instant one: `stop_reason="end_turn"`, one iteration, scored as a
successful instant turn. (The workaround of `tool_mode="brief"` with
`max_tool_iterations=1` ends as `max_iterations`: scored 0, recorded as a
failure memory and read by the dream and the tuner as a failed turn.)

- A turn that asked for no action makes no second call.
- A failed action goes back to the model as before, with or without the
  option.
- A host `on_after_tool_result` hook that returns a text still decides the
  reply; no second call is made.
- If the second call fails, or answers by asking for more tools, the text of
  the first call stays.
- The stored reply and `outcome.text` are the answer of the second call.
  With live token streaming, whatever the first call streamed before its
  actions ran is already on the screen, as in the tool loop.

The second call ends with one instruction, in English by default:

> The actions above have run. Read their results and answer the user now. Do
> not call any more tools.

Replace it per symbiote with the harness text `injection_instant_results`,
for instance to write it in the user's language. It is not an evolvable
component: the evolver never rewrites it.

```python
kernel.harness_versions.create_version(
    sym.id, "injection_instant_results", "As ações acima já rodaram. Leia os resultados e responda."
)
```

### A tool mode per session

The mode of the configuration is the one every session of the symbiote
starts with. One session can choose another for itself:

```python
kernel.set_session_tool_mode(session.id, "long_run")   # this conversation only
kernel.set_session_tool_mode(session.id, None)         # back to the symbiote's mode
```

- Values: the ones of the configuration (`"instant"`, `"brief"`,
  `"long_run"`, `"continuous"`, `"auto"`). `"auto"` is resolved at every
  turn, as in the configuration. Anything else raises `ValidationError`.
- It is stored with the session (`Session.tool_mode`; `None`: the
  symbiote's) and takes effect on the session's next turn. Two sessions of
  the same symbiote can run at the same time in different modes; changing
  one changes nothing for the others, and a new session starts in the
  symbiote's mode.
- It holds for the symbiote of the session only: a symbiote it delegates to
  (`spawn`) works in its own mode.
- The trace, `TurnOutcome.tool_mode` and the session score use the mode the
  turn ran in.
- Everything else the mode needs still comes from the configuration:
  `long_run` reads the symbiote's planner and evaluator settings, and the
  handoff of an unfinished project is injected into a session whose own mode
  is `long_run`.

Over HTTP it is `PUT /sessions/{id}/tool-mode` with `{"tool_mode": "brief"}`
(or `null`), and in the SDK `client.set_session_tool_mode(session_id, mode)`.

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

`context_mode` decides for the whole symbiote. To keep one knowledge entry
out of the packed prompt while the others stay in, register it with
`auto_recall=False`: it is never packed, and the model still finds it with
`search_knowledge` (authorize that tool in `tools` when the symbiote is in
`packed` mode).

```python
entry = kernel.knowledge.register_source(
    sym.id, "refund exceptions", content=text, auto_recall=False
)
kernel.knowledge.set_auto_recall(entry.id, True)   # change it later
```

### Preferences and constraints hold in every turn

In `packed` mode, memories reach the prompt by relevance: a memory enters
when it shares a term with the message. A preference ("prefers short
answers") or a constraint ("never book anything before 9am") is different:
it applies to every request, whatever its words. The memories of type
`preference` and `constraint` therefore enter every turn, the most important
first, up to `KernelConfig.standing_memory_limit` (default `5`; `0` restores
relevance only). They count against the same memory budget as the others,
and entering a turn updates their `last_used_at`, so the dream does not
prune them as unused. A host `MemoryPort` without `get_standing()` keeps the
relevance-only behaviour.

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

A memory that was deactivated (by the host, or by the dream's prune and
reconcile phases) can come back. `inspector.list_memories(...,
include_inactive=True)` shows them:

```python
kernel.memory.deactivate(memory_id, symbiote_id=sym.id)
kernel.memory.reactivate(memory_id, symbiote_id=sym.id)   # recalled again
```

`reactivate` follows the rules of `deactivate`: with `symbiote_id`, and with
`subject_key` for an end user's partition, an entry of another symbiote or of
another partition is not found.

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

### Skills on trial

A skill the agent writes starts on trial (status `quarantine`). By default
the model never sees a skill on trial: `<available-skills>` lists the active
ones and `skill_view` refuses the others, so a trial skill becomes active
only when the host promotes it (`symbiote skills promote`), and
`skill_auto_promote_threshold` has nothing to count.

```python
kernel.environment.configure(
    symbiote_id=sym.id,
    skill_injection_enabled=True,
    skill_trial_listing=True,          # default False
    skill_auto_promote_threshold=3,
    tools=[..., "skill_view"],
)
```

With `skill_trial_listing=True` the skills on trial are listed after the
active ones, marked `status="trial"`, with a line telling the model that they
were not reviewed yet. `skill_view` reads them (its answer says `"status":
"trial"`), and each read counts: at `skill_auto_promote_threshold` reads the
skill becomes active, unless `max_active_skills` is already reached. A skill
on trial that nobody uses is archived by the dream's prune phase after
`skill_quarantine_timeout_days`, so it only happens for symbiotes that dream
(`dream_mode`) or whose host calls `kernel.dream()`.

### Dream: which model, and knowing when a cycle ends

```python
kernel.environment.configure(symbiote_id=sym.id, dream_llm="main")   # default "aux"

def after_dream(symbiote_id, report, error):
    # report: the DreamReport of the cycle, or None when it failed (error is the exception)
    ...

kernel.set_dream_callback(after_dream)     # every cycle, automatic or kernel.dream()
kernel.wait_dream(sym.id, timeout=30)      # True once no automatic cycle is running
```

`dream_llm="aux"` calls the model given to `set_evolver_llm` (the main one
when none was set); `"main"` always calls the main model. An automatic cycle
runs in a background thread after a session closes: the callback runs in that
thread, before the cycle counts as finished, so `wait_dream` also waits for
it. An exception raised by the callback is logged and goes no further.

### Retention

`kernel.prune()` (or `symbiote prune`) applies the retention windows. Nothing
runs it automatically; call it from a cron. Every window is off until you set
it, in `KernelConfig` or as an argument of one call.

| Window (`KernelConfig` / `prune()` / CLI) | Removes |
|---|---|
| `message_retention_days` / `messages_days` / `--messages-days` | messages of sessions closed more than N days ago |
| `trace_retention_days` / `traces_days` / `--traces-days` | `execution_traces` and `turn_contexts` of those sessions |
| `summary_retention_days` / `summaries_days` / `--summaries-days` | the summary of sessions closed more than N days ago (set to empty; the session stays) |
| `audit_retention_days` / `audit_days` / `--audit-days` | rows of `audit_log`, `reflection_audit` and `skill_review_audit` created more than N days ago, and approval requests resolved more than N days ago, whatever their session |
| `session_retention_days` / `sessions_days` / `--sessions-days` | sessions closed more than N days ago, with everything tied to them |
| `memory_retention_days` / `memories_days` / `--memories-days` | memory entries created more than N days ago, whatever their session |
| `usage_retention_days` / `usage_days` / `--usage-days` | `llm_usage` rows created more than N days ago |

It returns what it deleted, one entry per table it can touch, plus
`summaries` (summaries cleared) and `sessions`. `dry_run=True` only counts,
and counts exactly what the real call deletes. The whole call is one
transaction.

**The message window does not remove the summary.** `close_session` stores a
summary of the session, and by default it is the text of the last five
messages. A host that removes messages for privacy either sets
`summary_retention_days` to the same number or configures the symbiote with
`session_summary_mode="off"`, so no summary is written.

**A session that is never closed.** The message and trace windows go by the
day the session was closed, so a host that keeps one session open per
conversation (`get_or_create_session`) would keep its messages for ever.
`retention_basis="age"` (`prune(basis="age")`, `--by-age`) makes the two
windows also remove every message and trace created more than N days ago, in
any session, the active ones included:

```python
KernelConfig(
    message_retention_days=30,
    trace_retention_days=30,
    retention_basis="age",        # default "closed"
    summary_retention_days=30,
    audit_retention_days=90,
    session_retention_days=90,
)
```

A turn reads the recent messages of its session, so use a window longer than
the history a conversation needs. The conversation goes on after the prune,
with the messages that are left.

**What the audit window covers.** `audit_log` has one row per tool call, with
its arguments (after your redactor and `secret_params`). The window goes by
the age of the row, so it also applies to sessions still open. An approval
request still waiting for a person is kept, however old; a resolved one counts
from the day it was resolved.

**What the session window covers.** The session row and the rows that hang on
it: messages, traces, turn contexts, the score, audit rows, approval
requests, decisions, artifacts. It only reaches closed sessions: close the
ones you consider over. Not removed: the memories learned in the session
(long-term memory, under `memory_retention_days`), the `llm_usage` rows
(counts, under `usage_retention_days`) and the scores already added to the
harness versions. A later `get_or_create_session` with the same
`external_key` starts a new session.

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

`loop_timeout` is checked before each round of the tool loop, against the
time since the loop began: it stops the turn with `stop_reason="timeout"`
and does not interrupt a model call or a tool in progress. By default that
time includes what the approval callback takes, so a person who is slow to
answer (see "Human-in-the-Loop") can end a turn that did almost no work.
`KernelConfig(loop_timeout_counts_approval=False)` takes the time spent in
the approval callback off the loop's clock: the limit then measures model
calls and tools only. It applies to the chat loop, sync and async, and to
each block of `tool_mode="long_run"`; the trace's `total_elapsed_ms` stays
wall-clock time. How long a person may take is the host's to bound, in its
own callback (or with `ApprovalDecision.defer`, which does not wait at all).

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

`kernel.on_shutdown(callback)` registers a function that `kernel.shutdown()`
calls before it stops the tool loop: for what an extension opened and must
release. `symbiote.browser.register` uses it to close its tabs and the
Chromium it launched. Callbacks run in registration order; one that raises is
logged and the others still run.

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
