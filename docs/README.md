# Symbiote v0.2.27 -- Documentation

## Architecture Overview

```
CLI / HTTP API / Python Library / SDK
        |
    SymbioteKernel (orchestrator)
        |
    CapabilitySurface (learn, teach, chat, work, show, reflect)
        |
    +-----------------------------------------------+
    |  ContextAssembler    RunnerRegistry            |
    |  ReflectionEngine    ProcessEngine             |  Cognitive Layer
    |  MemoryConsolidator  SubagentManager           |
    |  SkillsLoader        RuntimeContext            |
    +---+-------------------------------------------+
    +---+-------------------------------------------+
    |  IdentityManager     SessionManager            |
    |  MemoryStore         KnowledgeService          |  State Layer
    |  WorkspaceManager    EnvironmentManager         |
    |  SemanticRecallProvider  MessageRepository      |
    +---+-------------------------------------------+
    +---+-------------------------------------------+
    |  ToolGateway         PolicyGate                |
    |  LoopController      ChatRunner                |  Execution Layer
    |  SessionLock         CompositeHook             |
    +---+-------------------------------------------+
    +---+-------------------------------------------+
    |  ParameterTuner      HarnessEvolver            |
    |  HarnessVersionRepo  BenchmarkRunner           |  Harness Layer
    |  StructuralEvolver   CrossSymbioteLearner      |
    +---+-------------------------------------------+
    +---+-------------------------------------------+
    |  SQLiteAdapter       ForgeLLMAdapter           |  Adapter Layer
    |  ExportService       McpToolProvider           |
    +---+-------------------------------------------+
    +---+-------------------------------------------+
    |  SQLite DB  .  Filesystem  .  Docker           |  Persistence
    +-----------------------------------------------+
```

### Component Responsibilities

| Component | Package | Purpose |
|-----------|---------|---------|
| `SymbioteKernel` | `core/kernel.py` | Central orchestrator; composes all subsystems |
| `CapabilitySurface` | `core/capabilities.py` | 6 capabilities: learn, teach, chat, work, show, reflect |
| `ContextAssembler` | `core/context.py` | Budget-aware context assembly with tool/memory/knowledge |
| `ChatRunner` | `runners/chat.py` | LLM-driven conversational runner with tool loop |
| `LoopController` | `runners/loop_control.py` | Monitors loop health: stagnation, circuit breaker, max iterations |
| `ToolGateway` | `environment/tools.py` | Tool registry + policy-gated execution |
| `PolicyGate` | `environment/policies.py` | Deny-by-default tool authorization |
| `EnvironmentManager` | `environment/manager.py` | Per-symbiote configuration CRUD |
| `MemoryStore` | `memory/store.py` | Long-term memory persistence and retrieval |
| `ReflectionEngine` | `core/reflection.py` | Post-session fact extraction and summarization |
| `SessionLock` | `core/session_lock.py` | Per-session sync/async concurrency control |
| `CompositeHook` | `core/hooks.py` | Lifecycle hooks (before/after tool, before/after turn) |
| `HarnessEvolver` | `harness/evolver.py` | LLM-powered instruction text evolution |
| `ParameterTuner` | `harness/tuner.py` | Tiered auto-calibration of harness parameters |
| `HarnessVersionRepository` | `harness/versions.py` | Versioned evolvable texts with rollback |
| `BenchmarkRunner` | `harness/benchmark.py` | Automated task grading |
| `CrossSymbioteLearner` | `harness/cross_learning.py` | Transfer improvements between symbiotes |
| `StructuralEvolver` | `harness/structural.py` | Registry-based structural evolution |

---

## Kernel Public API Reference

All methods are on `SymbioteKernel`.

### Lifecycle

| Method | Signature | Description |
|--------|-----------|-------------|
| `__init__` | `(config: KernelConfig, llm: LLMPort \| None)` | Create kernel with config and optional LLM |
| `shutdown` | `() -> None` | Close storage adapter |

### Symbiote Management

| Method | Signature | Description |
|--------|-----------|-------------|
| `create_symbiote` | `(name: str, role: str, persona: dict \| None) -> Symbiote` | Create a new symbiote |
| `get_symbiote` | `(symbiote_id: str) -> Symbiote \| None` | Get by ID |
| `find_symbiote_by_name` | `(name: str) -> Symbiote \| None` | Find active symbiote by name |

### Session Management

| Method | Signature | Description |
|--------|-----------|-------------|
| `start_session` | `(symbiote_id: str, goal: str \| None, external_key: str \| None) -> Session` | Start new session |
| `get_or_create_session` | `(symbiote_id: str, external_key: str, goal: str \| None) -> Session` | Idempotent session by external key |
| `get_session` | `(session_id: str) -> Session \| None` | Resume existing session |
| `close_session` | `(session_id: str) -> Session` | Close with reflection + scoring |

### Messaging

| Method | Signature | Description |
|--------|-----------|-------------|
| `message` | `(session_id: str, content: str, extra_context: dict \| None) -> str` | Sync message (per-session locked) |
| `message_async` | `(session_id: str, content: str, extra_context: dict \| None, on_token: Callable \| None) -> str` | Async with streaming |

### Feedback

| Method | Signature | Description |
|--------|-----------|-------------|
| `report_feedback` | `(session_id: str, score: float, source: str) -> None` | Report user quality feedback (0.0-1.0) |

### Tool Loading

| Method | Signature | Description |
|--------|-----------|-------------|
| `load_discovered_tools` | `(symbiote_id: str, base_url: str) -> list[str]` | Load approved discovered tools |
| `load_mcp_tools` | `(registry: object, symbiote_id: str) -> list[str]` | Load MCP tools from forge-llm registry |
| `configure_tool_visibility` | `(symbiote_id: str, tags: list[str], loading: str, loop: bool) -> None` | Set tool visibility and loading mode |

### Harness Evolution

| Method | Signature | Description |
|--------|-----------|-------------|
| `set_evolver_llm` | `(llm: LLMPort) -> None` | Inject proposer LLM for evolution |
| `set_semantic_llm` | `(llm: LLMPort) -> None` | Inject cheap LLM for semantic tag resolution |
| `evolve_harness` | `(symbiote_id: str, component: str, default_text: str, days: int) -> EvolutionResult` | Run one evolution cycle |
| `check_harness_rollback` | `(symbiote_id: str, component: str) -> bool` | Auto-rollback if version underperforms |

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `capabilities` | `CapabilitySurface` | Access to learn/teach/chat/work/show/reflect |
| `hooks` | `CompositeHook` | Lifecycle hooks registration |
| `tool_gateway` | `ToolGateway` | Direct tool registry access |
| `environment` | `EnvironmentManager` | Per-symbiote configuration |
| `harness_versions` | `HarnessVersionRepository` | Version history access |
| `session_recall` | `SessionRecallPort \| None` | Host-provided session search |

---

## EnvironmentConfig Reference

All fields can be set via `kernel.environment.configure(symbiote_id=..., field=value)`.

| Field | Type | Default | Constraints | Description |
|-------|------|---------|-------------|-------------|
| `tools` | `list[str]` | `[]` | -- | Authorized tool IDs for this symbiote |
| `services` | `list[str]` | `[]` | -- | External service identifiers |
| `humans` | `list[str]` | `[]` | -- | Human contacts for escalation |
| `policies` | `dict` | `{}` | -- | Custom policy overrides |
| `resources` | `dict` | `{}` | -- | Resource configuration |
| `tool_tags` | `list[str]` | `[]` | -- | Filter which tools are visible by tag |
| `tool_loading` | `str` | `"full"` | `full\|index\|semantic` | How tool schemas appear in the prompt |
| `tool_mode` | `str` | `"brief"` | `instant\|brief\|long_run\|continuous` | Execution mode (see below) |
| `tool_loop` | `bool` | `True` | -- | (Deprecated) Derived from `tool_mode` |
| `prompt_caching` | `bool` | `False` | -- | Enable LLM prompt cache breakpoints |
| `memory_share` | `float` | `0.40` | 0.0-1.0 | Fraction of budget for memories |
| `knowledge_share` | `float` | `0.25` | 0.0-1.0 | Fraction of budget for knowledge |
| `max_tool_iterations` | `int` | `10` | 1-50 | Max tool loop iterations |
| `tool_call_timeout` | `float` | `30.0` | 1.0-300.0 | Per-tool call timeout (seconds) |
| `loop_timeout` | `float` | `300.0` | 10.0-3600.0 | Total loop timeout (seconds) |
| `context_mode` | `str` | `"packed"` | `packed\|on_demand` | Memory/knowledge injection strategy |

### Tool Loading Modes

- **full**: Complete JSON schemas in the system prompt. Best for small tool sets (< 20 tools).
- **index**: Compact one-line-per-tool catalog with a `get_tool_schema` meta-tool for lazy schema fetching. Best for medium tool sets (20-100).
- **semantic**: A cheap LLM pre-filters relevant tool tags per message before assembly. Best for large tool sets (100+). Requires `kernel.set_semantic_llm(llm)`.

### Execution Modes

Four modes for different task complexities:

- **instant**: Single-shot. One LLM call, 0-1 tool calls. Fast-path with mode-aware scoring. Best for: Q&A, simple queries.
- **brief**: Multi-step task loop. 3-10 iterations with compaction, scoring calibrated for compound tasks. Best for: "list clients + email + WhatsApp".
- **long_run**: Project-scale. Planner decomposes prompt into blocks, Generator executes each block, optional Evaluator grades output with host-defined criteria. Configurable context strategy (compaction/reset/hybrid). Best for: building applications, research projects.
- **continuous**: Always-on agent (placeholder). Purpose-driven, generates own objectives. Not yet implemented.

#### Long-run mode configuration

| Field | Type | Default | Description |
|---|---|---|---|
| `planner_prompt` | `str\|None` | `None` | Custom planner prompt (None = default or skip) |
| `evaluator_prompt` | `str\|None` | `None` | Custom evaluator prompt (None = skip evaluator) |
| `evaluator_criteria` | `list[dict]\|None` | `None` | Gradable criteria: `[{"name": "...", "weight": 1.0, "threshold": 0.7}]` |
| `context_strategy` | `str` | `"hybrid"` | `compaction\|reset\|hybrid` between blocks |
| `max_blocks` | `int` | `20` | Max work blocks per session |

### Context Modes

- **packed**: Memories and knowledge are injected into the context upfront (traditional RAG pattern).
- **on_demand**: Memories and knowledge are NOT injected upfront. Instead, `search_memories` and `search_knowledge` builtin tools are available for the LLM to call when needed. Reduces prompt size.

---

## Guides

- [QUICKSTART.md](../QUICKSTART.md) -- Installation, deployment modes, quick examples
- [docs/HARNESS_EVOLUTION.md](HARNESS_EVOLUTION.md) -- Self-evolving harness system guide
- [docs/HOST_INTEGRATION.md](HOST_INTEGRATION.md) -- Guide for hosts integrating Symbiote
- [project/docs/SPEC.md](../project/docs/SPEC.md) -- Full feature specification and sprint history
- [CHANGELOG.md](../CHANGELOG.md) -- Version history
