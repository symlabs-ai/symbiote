# Symbiote v0.2.27 -- Quickstart

Symbiote is a Python kernel for building **persistent AI agents**. It is not a framework -- it is an embeddable kernel that your application composes with its own LLM, tools, and business logic.

Where typical agent frameworks model **tasks** (stateless, ephemeral), Symbiote models **entities** -- instances with identity, layered memory, workspaces, tool environments, and self-reflection that persist across sessions.

## Installation

```bash
git clone <repo-url> symbiote
cd symbiote
python3.12 -m venv .venv
source .venv/bin/activate

# Core only
pip install -e .

# With dev tools (pytest, ruff, mypy)
pip install -e ".[dev]"

# With LLM support (forge-llm adapter)
pip install -e ".[dev,llm]"
```

Requirements: Python 3.12+, no external services (SQLite is bundled).

---

## Three Deployment Modes

### 1. Embedded Library (recommended)

The kernel runs inside your application process. Zero network overhead.

```python
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel

kernel = SymbioteKernel(
    config=KernelConfig(db_path="data/symbiote.db"),
    llm=your_llm_adapter,
)

sym = kernel.create_symbiote(name="Atlas", role="assistant")
session = kernel.start_session(sym.id, goal="Help with Python")
response = kernel.message(session.id, "Explain dataclasses")
kernel.close_session(session.id)
kernel.shutdown()
```

### 2. HTTP API Service

Runs as a standalone FastAPI service for multi-product architectures.

```bash
uvicorn symbiote.api.http:app --host 0.0.0.0 --port 8008
```

```bash
# Create a symbiote
curl -X POST http://localhost:8008/symbiotes \
  -H "Authorization: Bearer sk-symbiote_..." \
  -H "Content-Type: application/json" \
  -d '{"name": "Atlas", "role": "assistant"}'

# Send a message
curl -X POST http://localhost:8008/sessions/<SESSION_ID>/chat \
  -H "Authorization: Bearer sk-symbiote_..." \
  -H "Content-Type: application/json" \
  -d '{"content": "Explain dataclasses"}'
```

### 3. CLI

```bash
symbiote create --name "Atlas" --role "assistant"
symbiote session start <SYMBIOTE_ID> --goal "Python help"
symbiote --llm anthropic chat <SESSION_ID> "Explain dataclasses"
symbiote session close <SESSION_ID>
```

---

## Key Concepts

### Symbiote

A persistent cognitive entity with identity (name, role, persona), behavioral constraints, and its own configuration. Each symbiote has independent memory, environment, and harness settings.

### Session

A bounded conversation with a goal. Sessions contain messages, track decisions, and close with reflection (automatic fact extraction and summary). Sessions support external keys for idempotent get-or-create patterns.

### Memory (4 layers)

| Layer | Purpose | Persistence |
|-------|---------|-------------|
| Working Memory | Current conversation context | Session-scoped |
| Session Memory | Messages within a session | Session-scoped |
| Long-term Memory | Facts, preferences, procedures | Permanent |
| Semantic Recall | Keyword-scored retrieval | Query-time |

Memory entries are auto-classified into categories: `ephemeral`, `declarative`, `procedural`, `meta`. Types include `working`, `preference`, `constraint`, `factual`, `procedural`, `decision`, `reflection`, and more.

### Tools and Environment

Tools are registered in the `ToolGateway` and authorized per-symbiote via `EnvironmentManager`. The kernel supports three tool sources:

- **Built-in tools**: `fs_read`, `fs_write`, `fs_list`, `search_memories`, `search_knowledge`
- **HTTP tools**: Declarative REST endpoint wrappers via `HttpToolConfig`
- **MCP tools**: Model Context Protocol tools bridged via forge-llm

### Harness Evolution

The self-improving system that makes Symbiote unique. See [docs/HARNESS_EVOLUTION.md](docs/HARNESS_EVOLUTION.md) for the full guide.

---

## Configuration

### KernelConfig (global)

```python
from symbiote.config.models import KernelConfig

config = KernelConfig(
    db_path="data/symbiote.db",  # SQLite database path
    context_budget=4000,          # Token budget for context assembly
    llm_provider="forge",         # LLM provider identifier
    log_level="INFO",             # DEBUG, INFO, WARNING, ERROR, CRITICAL
)
```

### EnvironmentConfig (per-symbiote)

Configured via `kernel.environment.configure()` or `EnvironmentManager`. All fields have sensible defaults.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `tools` | `list[str]` | `[]` | Authorized tool IDs |
| `tool_tags` | `list[str]` | `[]` | Filter tools by tag |
| `tool_loading` | `"full"\|"index"\|"semantic"` | `"full"` | How tool schemas appear in prompts |
| `tool_mode` | `"instant"\|"brief"\|"continuous"` | `"brief"` | Tool loop behavior (see below) |
| `max_tool_iterations` | `int` | `10` | Max tool loop iterations (cap: 50) |
| `tool_call_timeout` | `float` | `30.0` | Per-tool call timeout in seconds |
| `loop_timeout` | `float` | `300.0` | Total loop timeout in seconds |
| `context_mode` | `"packed"\|"on_demand"` | `"packed"` | Memory/knowledge injection mode |
| `memory_share` | `float` | `0.40` | Fraction of budget for memories |
| `knowledge_share` | `float` | `0.25` | Fraction of budget for knowledge |
| `prompt_caching` | `bool` | `False` | Enable LLM prompt caching |

---

## Tool System

### Registering HTTP Tools

```python
from symbiote.environment.descriptors import HttpToolConfig, ToolDescriptor

kernel.tool_gateway.register(
    descriptor=ToolDescriptor(
        tool_id="get_weather",
        name="Get Weather",
        description="Get current weather for a city.",
        parameters={
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name"},
            },
            "required": ["city"],
        },
        tags=["weather"],
        risk_level="low",  # "low", "medium", "high"
    ),
    http_config=HttpToolConfig(
        method="GET",
        url_template="https://api.weather.com/v1/current?city={city}",
        headers={"X-API-Key": "..."},
    ),
)

# Authorize tools for a symbiote
kernel.environment.configure(symbiote_id=sym.id, tools=["get_weather"])
```

### Loading MCP Tools

```python
from forge_llm.application.tools import McpToolset

async with McpToolset.from_http("http://localhost:9000/mcp") as registry:
    tool_ids = kernel.load_mcp_tools(registry, symbiote_id=sym.id)
```

### Tool Mode

| Mode | Behavior |
|------|----------|
| `instant` | Single-shot: call tool once, return result |
| `brief` | Loop up to `max_tool_iterations` (default 10) |
| `continuous` | Loop up to `max_tool_iterations` (default 50, higher cap) |

### Risk Level and Human-in-the-Loop

Tools can declare `risk_level` (`"low"`, `"medium"`, `"high"`). The host provides an `on_before_tool_call` callback that can approve or reject tool calls:

```python
def approval_callback(tool_id: str, params: dict, risk: str) -> bool:
    if risk == "high":
        return ask_user_for_confirmation(tool_id, params)
    return True  # auto-approve low/medium

runner = ChatRunner(
    llm=llm,
    tool_gateway=kernel.tool_gateway,
    on_before_tool_call=approval_callback,
)
```

---

## Memory System

### On-Demand Mode

When `context_mode="on_demand"`, memories and knowledge are not injected into the context upfront. Instead, the LLM can call `search_memories` and `search_knowledge` tools to retrieve what it needs. This reduces prompt size for symbiotes with large memory stores.

```python
kernel.environment.configure(
    symbiote_id=sym.id,
    context_mode="on_demand",
)
```

### Memory Categories

Each memory type is auto-classified into a category:

- **ephemeral**: working memory (session-scoped, auto-expires)
- **declarative**: facts, preferences, constraints
- **procedural**: how-to knowledge, workflows
- **meta**: summaries, reflections, notes about other memories

---

## Agent Loop Features

The `ChatRunner` implements a resilient agent loop with:

- **Parallel tool execution**: Independent tool calls run concurrently via `ThreadPoolExecutor` (sync) or `asyncio.gather` (async)
- **LLM retry with backoff**: Transient errors retry up to 3 times with exponential backoff (1s, 2s, 4s)
- **Stagnation detection**: Duplicate tool calls (same tool + same params) trigger a stop message
- **Circuit breaker**: 3 consecutive failures on the same tool trigger an immediate stop
- **3-layer compaction**: (1) microcompact truncates tool results > 2000 chars, (2) loop compaction summarizes old pairs after 4 iterations, (3) autocompact at 80% of context budget
- **Streaming**: `on_token`, `on_stream(text, iteration)`, `on_progress(event, iteration, total)` callbacks
- **Per-tool and loop timeouts**: Configurable via `EnvironmentConfig`

---

## Harness Evolution System

Symbiote includes a self-evolving harness that automatically improves agent behavior based on session data. This is the Meta-Harness system.

For the complete guide, see [docs/HARNESS_EVOLUTION.md](docs/HARNESS_EVOLUTION.md).

Key components:

- **SessionScore**: Automatic 0.0-1.0 quality scoring from loop traces
- **FeedbackPort**: Host reports user satisfaction, combined with auto-score
- **ParameterTuner**: Tiered auto-calibration (Tier 0-3) of harness parameters
- **HarnessEvolver**: LLM-powered evolution of instruction texts
- **HarnessVersionRepository**: Versioned text storage with rollback
- **BenchmarkRunner**: Automated task grading for regression testing
- **CrossSymbioteLearner**: Transfer improvements between similar symbiotes

---

## Context Assembly

The `ContextAssembler` builds a ranked, budget-aware context payload:

1. Load persona from `IdentityManager`
2. Get working memory snapshot
3. Get relevant memories (sorted by importance)
4. Get relevant knowledge
5. Resolve tool descriptors (full, index, or semantic mode)
6. Trim to fit token budget (configurable memory/knowledge shares)
7. Resolve evolvable text overrides from `harness_versions`

The assembled context drives the ChatRunner's system prompt, tool instructions, and loop behavior.

---

## Host Integration

For a complete guide on integrating Symbiote into your application, see [docs/HOST_INTEGRATION.md](docs/HOST_INTEGRATION.md).

Quick reference:

```python
# Initialize
kernel = SymbioteKernel(config=KernelConfig(...), llm=llm)

# Register tools
kernel.tool_gateway.register(descriptor, http_config=config)
kernel.environment.configure(symbiote_id=sid, tools=[...])

# Session management
session = kernel.get_or_create_session(sid, external_key="user:123")

# Send message with context injection
response = kernel.message(session.id, "...", extra_context={"url": "..."})

# Async with streaming
response = await kernel.message_async(
    session.id, "...", on_token=lambda t: send_sse(t)
)

# Report feedback
kernel.report_feedback(session.id, score=0.9, source="thumbs_up")

# Close session (triggers reflection + scoring)
kernel.close_session(session.id)
```

---

## Benchmark Suite

Test symbiote behavior with automated grading:

```python
from symbiote.harness.benchmark import BenchmarkRunner, BenchmarkTask

runner = BenchmarkRunner(kernel)
suite_result = runner.run_suite(
    symbiote_id=sym.id,
    tasks=[
        BenchmarkTask(
            id="weather-lookup",
            description="What is the weather in Tokyo?",
            expected_tools=["get_weather"],
            grading="tool_called",
        ),
    ],
    suite_name="smoke",
)
print(f"Passed: {suite_result.passed}/{suite_result.total_tasks}")
```

---

## Cross-Symbiote Learning

When one symbiote discovers effective instructions, similar symbiotes can benefit:

```python
from symbiote.harness.cross_learning import CrossSymbioteLearner

learner = CrossSymbioteLearner(kernel._storage, kernel.harness_versions)
candidates = learner.find_candidates(target_symbiote_id=sym.id, min_overlap=0.5)

for transfer in candidates:
    learner.transfer(transfer)
```

See [docs/HARNESS_EVOLUTION.md](docs/HARNESS_EVOLUTION.md) for details.
