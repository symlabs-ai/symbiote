# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/)

## [v0.3.4] - 2026-04-01

- feat: API config endpoint — PUT/GET /symbiotes/{id}/config (tool_mode, long-run config, timeouts)
- docs: Clark migration guide — execution modes, routing strategies, long-run editorial example
- docs: update all docs for 4-mode execution taxonomy

---

## [v0.3.3] - 2026-04-01

- feat: long-run mode — Planner/Generator/Evaluator architecture
- docs: harness_plan — 4-mode taxonomy, long-run design, continuous concept

---

## [v0.3.2] - 2026-04-01

- feat: brief mode — sync trace, calibrated scoring, multi-step instructions

---

## [v0.3.1] - 2026-04-01

- feat: instant mode — mode-aware harness for single-shot execution
- docs: revise harness_plan.md — mark phases 1-4 as implemented, add future work from Anthropic article
- docs: Clark migration guide for v0.3.0 — 5 adoption levels

---

## [v0.3.0] - 2026-04-01

### Novas funcionalidades

- **Self-Evolving Harness**: Complete Meta-Harness system inspired by Stanford/CMU paper. SessionScore auto-computes quality from LoopTrace (stop_reason + iterations + failure rate). FeedbackPort lets hosts report user satisfaction. ParameterTuner auto-calibrates harness parameters with tiered activation (Tier 0-3). HarnessEvolver uses a proposer LLM to evolve prompt texts with guard rails and auto-rollback. Three evolvable components: tool_instructions, injection_stagnation, injection_circuit_breaker.
- **Harness Versioning**: harness_versions table tracks text variants per symbiote with score tracking, rollback chain, and version history. ChatRunner resolves active versions via ContextAssembler.
- **Agent Loop Resilience**: Parallel tool execution (asyncio.gather + ThreadPoolExecutor), LLM retry with exponential backoff (3 retries, 1s/2s/4s), diminishing returns detection (stagnation + circuit breaker via LoopController), 3-layer compaction (microcompact + loop compact + autocompact).
- **Timeout System**: Per-tool timeout (default 30s) and loop timeout (default 300s), both configurable per symbiote via EnvironmentConfig.
- **Human-in-the-Loop**: risk_level (low/medium/high) on ToolDescriptor, on_before_tool_call approval callback on ChatRunner. High-risk tools require explicit approval when callback is set.
- **Tool Mode**: tool_mode (instant/brief/continuous) replaces binary tool_loop. Instant = single-shot, brief = configurable loop (default), continuous = placeholder for future autonomous agents.
- **Streaming Mid-Loop**: on_progress(event, iteration, total) and on_stream(text, iteration) callbacks for real-time loop visibility. on_token behavior unchanged (final response only).
- **Working Memory Summary**: Loop execution summary prepended to WorkingMemory after tool calls, enabling multi-turn awareness of previous tool steps.
- **Memory On-Demand**: context_mode (packed/on_demand) per symbiote. search_memories and search_knowledge as builtin tools. On-demand mode skips pre-packed context injection.
- **Index Mode Cache**: Loop-local schema cache avoids redundant get_tool_schema calls in index mode, reducing iterations by ~50%.
- **Benchmark Suite**: BenchmarkRunner with task grading (tool_called, param_match, custom). Automated evaluation of symbiote performance.
- **Structural Evolution**: StructuralEvolver with pluggable strategy registry for code-level harness changes.
- **Cross-Symbiote Learning**: CrossSymbioteLearner detects tool overlap between symbiotes and transfers harness versions.
- **Multi-Model Test Matrix**: E2E test infrastructure with 3 scenarios across multiple models, collecting iteration/success/elapsed metrics.
- **MemoryEntry de Falha**: Deterministic procedural memory generated when tool loop fails (circuit_breaker, stagnation, max_iterations). Zero LLM cost.
- **Configurable Context Splits**: memory_share and knowledge_share per symbiote via EnvironmentConfig (defaults 0.40/0.25).
- **LoopTrace Persistence**: execution_traces table stores full trace (steps, stop_reason, timing) for observability and harness evolution.

### Melhorias

- **ContextAssembler**: Resolves evolvable text overrides from harness_versions, configurable memory/knowledge splits, context_mode support, timeout/tool_mode propagation
- **ChatRunner**: Accepts on_progress, on_stream, on_before_tool_call callbacks; uses _resolve_max_iters for tool_mode; integrates schema cache, approval gate, and timeout
- **LoopController**: Accepts custom stagnation and circuit_breaker messages for prompt evolution
- **EnvironmentConfig**: 16 configurable fields including tool_mode, context_mode, timeouts, splits, max_tool_iterations
- **ToolGateway**: register_memory_tools(), get_risk_level(), timeout support in execute/execute_async

### Correções

- run_async() now uses _call_llm_with_retry (was bypassing retry logic)
- E2E tool_results assertions relaxed for loop-aware behavior

### Documentação

- Complete QUICKSTART.md rewrite for v0.3.0
- New docs/HARNESS_EVOLUTION.md developer guide
- New docs/HOST_INTEGRATION.md for host developers
- Updated docs/README.md with architecture, API reference, config reference
- Updated SPEC.md with Execution Layer + Harness Layer in architecture diagram

### Outros

- 1184 tests (up from ~900), including 130+ new tests for harness features
- Multi-model E2E test infrastructure (skipable via SYMBIOTE_E2E_LLM=1)
- harness/ package: versions.py, tuner.py, evolver.py, benchmark.py, structural.py, cross_learning.py

---

## [v0.2.27] - 2026-04-01

### Added — Final Horizon Sprint

- [B-33] Per-tool timeout (30s default) + loop timeout (300s default) configurable per symbiote (`environment/tools.py`, `runners/chat.py`)
- [B-29] Human-in-the-loop — `risk_level` on ToolDescriptor + `on_before_tool_call` approval callback on ChatRunner (`environment/descriptors.py`, `runners/chat.py`)
- [B-34] Index mode schema cache — loop-local cache avoids redundant get_tool_schema calls (`runners/chat.py`)
- [B-35] Multi-model test matrix — E2E infrastructure with 3 scenarios across 3 models (`tests/e2e/test_multi_model.py`)
- [B-40] Tool Mode — `tool_mode: Literal["instant", "brief", "continuous"]` replaces binary `tool_loop` (`core/models.py`, `runners/chat.py`)
- [B-27] Streaming mid-loop — `on_progress(event, iter, total)` + `on_stream(text, iter)` callbacks (`runners/chat.py`)
- [B-30] Working memory intermediária — loop summary prepended to WorkingMemory assistant message (`runners/chat.py`)
- [B-68] Memory/Knowledge on-demand — `context_mode: packed|on_demand`, `search_memories`/`search_knowledge` builtin tools (`environment/tools.py`, `core/context.py`)
- [H-11] BenchmarkRunner — task grading (tool_called, param_match, custom) (`harness/benchmark.py`)
- [H-12] StructuralEvolver — pluggable strategy registry with proposal/apply (`harness/structural.py`)
- [H-13] CrossSymbioteLearner — tool overlap detection + harness version transfer (`harness/cross_learning.py`)

## [v0.2.26] - 2026-04-01

### Added — Prompt Evolution (Meta-Harness Fase 3)

- [B-67] HarnessEvolver — LLM proposer analyzes session traces (failed vs successful) and proposes improved harness texts; guard rails (max 2x length, CRITICAL preservation, format check); auto-rollback if score drops after 50 sessions (`harness/evolver.py`)
- [B-67] Evolvable text bridge — `AssembledContext` gains `tool_instructions_override`, `injection_stagnation_override`, `injection_circuit_breaker_override`; `ContextAssembler` resolves from `harness_versions`; `ChatRunner` and `LoopController` use overrides with fallback to defaults (`core/context.py`, `runners/chat.py`, `runners/loop_control.py`)
- [B-67] `kernel.set_evolver_llm(llm)` — host injects separate proposer LLM (option 3: accepts both, default to main LLM); `kernel.evolve_harness()` and `kernel.check_harness_rollback()` for batch invocation (`core/kernel.py`)

### Changed

- `LoopController` accepts `stagnation_msg` and `circuit_breaker_msg` parameters for customizable injection messages
- `_persist_score()` now tracks score per active harness version via `update_score()` for evolution rollback decisions
- 3 evolvable components defined: `tool_instructions`, `injection_stagnation`, `injection_circuit_breaker`

## [v0.2.25] - 2026-04-01

### Added — Harness Evolution (Meta-Harness Fase 2)

- [B-32/B-65] max_tool_iterations configurable — per symbiote via EnvironmentConfig (default 10, cap 50); propagates through ContextAssembler → AssembledContext → ChatRunner (`core/models.py`, `core/context.py`, `runners/chat.py`)
- [B-64] harness_versions table + HarnessVersionRepository — version evolvable texts per symbiote with score tracking and rollback (`harness/versions.py`, `adapters/storage/sqlite.py`)
- [B-65] ParameterTuner — tiered auto-calibration: Tier 0 (0 sessions, no change), Tier 1 (5+, safe only), Tier 2 (20+, statistical), Tier 3 (50+, fine tuning). Rules: max_iterations adjustment, compaction threshold, memory share. Safety caps + logging (`harness/tuner.py`)

### Changed

- `ChatRunner.run()`/`run_async()` now read `context.max_tool_iterations` instead of `_MAX_TOOL_ITERATIONS` constant
- `EnvironmentManager.configure()` accepts `max_tool_iterations` parameter

## [v0.2.24] - 2026-04-01

### Added — Harness Foundations (Meta-Harness Fase 1)

- [B-60] SessionScore — `compute_auto_score(trace)` computes 0.0-1.0 score from LoopTrace (stop_reason + iterations + failure rate); persisted in `session_scores` table on `close_session()` (`core/scoring.py`)
- [B-61] FeedbackPort — protocol for host to report session quality; `kernel.report_feedback(session_id, score, source)` updates `final_score = auto * 0.6 + user * 0.4` (`core/ports.py`, `core/kernel.py`)
- [B-62] MemoryEntry de falha — deterministic procedural memory generated when loop fails (circuit_breaker, stagnation, max_iterations); zero LLM cost, tagged `[harness_failure]` (`core/kernel.py`)
- [B-63] Context splits configuráveis — `memory_share` and `knowledge_share` per symbiote via EnvironmentConfig; defaults 0.40/0.25 preserved (`environment/manager.py`, `core/context.py`)
- [B-66] LoopTrace persistence — `execution_traces` table stores full trace (steps, stop_reason, timing); `CapabilitySurface` captures `last_loop_trace` from RunResult (`adapters/storage/sqlite.py`, `core/capabilities.py`)

### Changed

- `CapabilitySurface.chat()` and `chat_async()` now capture `loop_trace` from RunResult
- `kernel._message_inner()` persists trace to `execution_traces` after each chat call
- `kernel.close_session()` computes SessionScore + generates failure MemoryEntry before reflection
- `ContextAssembler._trim_to_budget()` uses per-symbiote memory/knowledge shares

## [v0.2.23] - 2026-04-01

- docs: clean up BACKLOG — remove 8 implemented items (B-25/26/28/31/37/38/39/43), add 9 Meta-Harness items (B-60 to B-68)
- docs: add Meta-Harness analysis to kb/

## [v0.2.22] - 2026-03-31

### Added — Agent Loop Resilience Sprint

- [B-55] Parallel tool execution — `ToolGateway.execute_tool_calls()` uses `ThreadPoolExecutor(max_workers=4)` for sync, `asyncio.gather()` for async; one failing tool does not block others (`environment/tools.py`)
- [B-56] LLM retry with exponential backoff — `ChatRunner._call_llm_with_retry()` retries transient errors (ConnectionError, TimeoutError, rate limits, 5xx) up to 3 times with 1s/2s/4s delays (`runners/chat.py`)
- [B-57] Diminishing returns detection + circuit breaker — `LoopController` monitors duplicate calls (same tool+params 2x), circuit breaker (same tool fails 3x), and injects stop message for clean LLM exit (`runners/loop_control.py`)
- [B-58] 3-layer compaction — Layer 1: microcompact (truncate tool results >2000 chars); Layer 2: loop compaction (summarize old pairs after 4 iterations); Layer 3: autocompact (aggressive compact when tokens >80% of context budget) (`runners/chat.py`)

### Changed

- [B-57] `LoopTrace` gains `stop_reason` field (end_turn, max_iterations, stagnation, circuit_breaker) (`runners/base.py`)
- [B-58] `ChatRunner` gains `context_budget` parameter (default 16000 tokens) for autocompact threshold
- [B-58] `_format_tool_results()` now applies microcompact to each individual result before injection

## [v0.2.21] - 2026-03-30

- feat: Hermes adaptations — SessionRecallPort, MemoryCategory, context compaction
- docs: comprehensive update — host integration guide, architecture, changelogs

## [v0.2.20] - 2026-03-30

### Added — Nanobot Report Adaptations

- [B-46] Prompt cache integration — `EnvironmentConfig.prompt_caching` flag propagates `prompt_caching=True` to forge_llm, enabling Anthropic cache breakpoints (~90% token saving)
- [B-47] Message retry with exponential backoff — `MessageBus` retries handler failures up to `max_retries` (default 3) with backoff (1s, 2s, 4s); `respond()` retries on QueueFull
- [B-48] Per-session locks — `SessionLock` provides sync/async per-session locking in `kernel.message()`; different sessions run in parallel, same session serializes
- [B-50] CompositeHook — composable lifecycle hooks (`before_tool`, `after_tool`, `before_turn`, `after_turn`) with error isolation per-hook
- [B-51] Delta streaming — `StreamDelta` event + `send_delta()`/`receive_delta()` in MessageBus for progressive token delivery to channels

### Added — Hermes Report Adaptations

- [B-52] SessionRecallPort — protocol for host-provided session search; kernel defines contract, host implements (FTS5, embeddings, etc.)
- [B-53] MemoryCategory — auto-classification of memories (ephemeral, declarative, procedural, meta) with `MEMORY_TYPE_CATEGORY` mapping; `get_by_category()` query method
- [B-54] Context compaction mid-loop — replaces old tool-loop message pairs with compact summary after 4+ iterations; prevents context growth during multi-step execution

### Changed

- [B-49] `HttpToolConfig.allow_internal` excluded from serialization (`model_dump()`) — can only be set programmatically in code, never from config/API/DB. Added audit log warning when SSRF bypass is active
- [B-53] ReflectionEngine now stores extracted facts with their actual type (preference, constraint, procedural) instead of generic "reflection"

## [0.2.5] — 2026-03-19

### Added — Tool Loop (agentic multi-step execution)

- `ChatRunner` tool loop — when `tool_loop=True` (default), the runner iterates: LLM → parse tool calls → execute → feed results back → LLM, until the model responds without tool calls or hits `max_iterations` (default 10). Previously the LLM was blind after the first tool call (single-shot).
- `_format_tool_results()` — formats tool execution results as structured messages injected back into the conversation for the next LLM turn
- `_format_assistant_with_calls()` — preserves the assistant's tool call text in conversation history so the LLM sees the full chain of reasoning
- Working memory only stores the **final** response, not intermediate tool-calling turns
- `RunResult.output` returns `{"text": ..., "tool_results": [...]}` when tools were executed, preserving full audit trail
- `run_async()` — async variant with identical loop semantics, uses `execute_tool_calls_async()`

### Added — Semantic Tool Loading

- `ContextAssembler` now supports three tool loading modes via `EnvironmentConfig.tool_loading`:
  - **full** — complete tool schemas in system prompt (existing behavior)
  - **index** — compact one-line-per-tool catalog with a `get_tool_schema` meta-tool for lazy schema fetching
  - **semantic** — LLM-powered pre-filter resolves relevant tool tags before context assembly, minimizing prompt size
- `ToolTagResolver` (`environment/resolver.py`) — uses a cheap/fast LLM to select relevant tool tags from the user query, reducing the tool set sent to the main LLM
- `EnvironmentConfig.tool_loading: Literal["full", "index", "semantic"]` — persisted per-symbiote
- `EnvironmentConfig.tool_loop: bool` — toggle agentic loop on/off per-symbiote
- `EnvironmentConfig.tool_tags: list[str]` — filter tools by tag for scoped visibility
- `EnvironmentManager.get_tool_loading()`, `get_tool_loop()`, `get_tool_tags()` — accessors with SQLite persistence
- `PUT/GET /symbiotes/{id}/tool-tags` — REST endpoints for tool loading configuration
- `kernel.configure_tool_visibility()` — unified API for setting tags, loading mode, and loop toggle

### Added — ToolGateway enhancements

- `ToolGateway.execute_tool_calls()` — batch execution accepting `list[ToolCall]`, returns `list[ToolCallResult]`
- `ToolGateway.execute_tool_calls_async()` — async batch variant
- `ToolGateway.get_tool_schema(tool_id)` — returns full schema dict for a single tool (used by index mode's meta-tool)
- `ToolGateway.list_tags()` — returns deduplicated set of all registered tool tags
- `ToolGateway.get_descriptors_by_tags(tags)` — filter registered tools by tag list
- `ToolCallResult` model — structured result with `tool_id`, `success`, `output`, `error`

### Added — Discovery enhancements

- `DiscoveredTool.handler_type` field — distinguishes HTTP vs CLI vs custom discovered tools
- `DiscoveryService` FastAPI strategy now extracts response models and query parameters
- `DiscoveredToolRepository` upsert preserves `handler_type` across re-scans

### Changed

- `ChatRunner.run()` refactored from single-shot to iterative loop (backward compatible: `tool_loop=False` restores single-shot)
- `ChatRunner._build_system()` now includes brief tool-loop instructions when `tool_loop=True`
- `AssembledContext` gains `tool_loading`, `tool_loop`, `available_tools` fields
- SQLite schema: `env_configs` table gains `tool_loading`, `tool_loop`, `tool_tags` columns (idempotent ALTER TABLE)

### Tests

- 794 tests passing (+170 new)
- `test_chat_runner_tools.py` — tool loop iterations, max_iterations guard, async loop, tool results accumulation
- `test_context.py` — all three loading modes (full/index/semantic), token budget with tools
- `test_tool_gateway.py` — batch execution, tag filtering, schema retrieval, async execution
- `test_environment.py` — tool_loading/tool_loop/tool_tags persistence round-trip
- `test_loading_modes.py` — 241 realistic tools (YouNews-like), semantic filtering with mock LLM
- `test_resolver.py` — ToolTagResolver unit tests

## [0.2.4] — 2026-03-18

### Added — B-23: Deploy Hosted (DevOps)

- Porta 8008 alocada no port-registry
- `symbiote.service` systemd unit rodando em produção
- Nginx + SSL via certbot em `symbiote.symlabs.ai`
- CI/CD via `.gitea/workflows/staging-deploy.yml` — push na main → pull + restart automático
- Deploy prod via `promote.sh`

### Added — B-7: MCP Integration

- `symbiote.mcp.provider.McpToolProvider` — bridges a live `forge_llm.application.tools.ToolRegistry` (produced by `McpToolset`) into Symbiote's `ToolGateway`; each MCP tool is registered as an async custom handler that delegates to `McpTool.execute_async()`
- `SymbioteKernel.load_mcp_tools(registry, symbiote_id)` — convenience method: loads all tools from a forge_llm ToolRegistry and auto-authorizes them via `EnvironmentManager.configure()`
- Tool names are sanitized (hyphens and spaces → underscores) to produce valid tool_ids
- MCP errors (`result.is_error`) surface as `RuntimeError` so PolicyGate captures them as failed tool results
- Supports stdio and HTTP transports via forge_llm's `McpToolset.from_stdio()` / `McpToolset.from_http()` / `McpToolset.from_servers()`

### Added — B-24: DiscoveredToolLoader

- `symbiote.discovery.loader.DiscoveredToolLoader` — reads approved discovered tools from SQLite and registers them as HTTP tools in `ToolGateway` with `allow_internal=True`; resolves `{base_url}` placeholder and skips CLI tools (`handler_type=custom`) or tools without `method`/`url_template`
- `SymbioteKernel.load_discovered_tools(symbiote_id, base_url)` — single call to load and auto-authorize discovered tools via `EnvironmentManager.configure()`, closing the loop: `discover → approve → kernel uses`

### Fixed

- SQLite `check_same_thread=False` in `SymbioteKernel.__init__` (prevented thread errors in asyncio context)
- Dev mode auth bypass (`SYMBIOTE_DEV_MODE=1`) now unconditional — checked before `key_manager` initialization to prevent 401 on second request

## [0.2.3] — 2026-03-18

### Added — Discovery Service (sprint-discovery-service)

- `symbiote.discovery.models.DiscoveredTool` — Pydantic model for tools found by scanning a repository (status: pending/approved/disabled)
- `symbiote.discovery.repository.DiscoveredToolRepository` — SQLite-backed CRUD for discovered tools; upsert preserves approval status across re-scans
- `symbiote.discovery.service.DiscoveryService.discover()` — scans a repository using 4 strategies: OpenAPI/Swagger specs, FastAPI decorators, Flask decorators, pyproject.toml scripts; deduplicates by tool_id
- `SQLiteAdapter` schema: `discovered_tools` table with unique constraint on `(symbiote_id, tool_id)` and index on `(symbiote_id, status)`
- REST API: `POST /symbiotes/{id}/discover`, `GET /symbiotes/{id}/discovered-tools?status=`, `PATCH /symbiotes/{id}/discovered-tools/{tool_id}`, `DELETE /symbiotes/{id}/discovered-tools/{tool_id}`
- CLI `symbiote init` — creates or links a symbiote on a remote server, writes `.symbiote/config` with server URL, API key, symbiote ID and name
- CLI `symbiote discover [path]` — scans a local repository and registers tools; displays Rich table of discovered tools with method, endpoint and source file
- Dashboard "Discovered Tools" section — lists all discovered tools across symbiotes with method, endpoint, status badge and approve/disable toggle; two new stat cards (Tools total, Pending); Quick Reference updated with discovery endpoints
- `GET /api/dashboard` now returns `discovered_tools` list and `stats.discovered_tools` / `stats.pending_tools`

## [0.2.2] — 2026-03-18

### Added — Internal Tools & Async Streaming (YouNews feedback)

- `HttpToolConfig.allow_internal` — opt-in flag to bypass SSRF validation for tools that intentionally call loopback/private-network endpoints (e.g. same-host services); default `False` preserves existing protection
- `kernel.message_async(session_id, content, on_token=...)` — async entry point for chat; eliminates manual `ContextVar`/`emit_event` workarounds in SSE integrations
- `CapabilitySurface.chat_async()` — async variant of `chat()` that propagates `on_token` down to the runner
- `on_token` callback in `ChatRunner.run()` and `run_async()`: called per-token when LLM exposes `stream()`, or once with full response as fallback

## [0.2.1] — 2026-03-18

### Added — Dynamic Auth Headers & Async Tool Handlers (YouNews feedback)

- `HttpToolConfig.header_factory` — callable invoked per-request to supply dynamic headers (e.g. user-scoped auth tokens); eliminates `threading.local` workarounds in host integrations
- `PolicyGate.execute_with_policy_async()` — async policy execution: awaits coroutine handlers, wraps sync handlers via `asyncio.to_thread`
- `ToolGateway.execute_async()` / `execute_tool_calls_async()` — async execution path for tool calls
- `ChatRunner.run_async()` — async runner variant that uses `execute_tool_calls_async()`, resolving single-worker event-loop deadlocks when tools call the same uvicorn process

## [0.2.0] — 2026-03-17

### Added — Native Function Calling

- `LLMResponse` model — structured return type for LLM adapters with optional `tool_calls` field
- `NativeToolCall` model — represents a provider-native tool call with `call_id`, `tool_id`, `params`
- `ToolDescriptor.to_openai_schema()` — converts tool descriptors to OpenAI function calling format
- `LLMPort.complete()` now accepts optional `tools` parameter for native tool definitions
- `ChatRunner(native_tools=True)` — opt-in flag to use native function calling instead of text-based parsing
- When `native_tools=True`, tool definitions are passed to the LLM via the `tools` parameter and text-based `tool_call` instructions are omitted from the system prompt
- Full backward compatibility: adapters returning `str` continue to work via text-based parsing

### Changed

- `LLMPort.complete()` signature expanded: `tools: list[dict] | None = None` parameter added
- `ChatRunner` detects `LLMResponse` vs `str` return type automatically

## [0.1.8] — 2026-03-17

### Added — Hosted Service (API + SDK)

- [B-19] API Key Authentication — Bearer token auth with SHA-256 hashed keys, tenant isolation, admin/user roles (`api/auth.py`, `api/middleware.py`)
- [B-20] Chat Endpoint — `POST /sessions/{id}/chat` calls `kernel.message()` with LLM + tools via HTTP API (`api/http.py`)
- [B-21] Multi-tenant Isolation — `owner_id` set on symbiote creation, tenant check on chat and get endpoints
- [B-22] Python SDK — `SymbioteClient` thin HTTP client with httpx, context manager, full API coverage (`sdk/client.py`)
- Admin endpoints: `POST/GET/DELETE /admin/api-keys` for key lifecycle management
- Dev mode: `SYMBIOTE_DEV_MODE=1` env var for local development without auth

### Changed

- All mutation endpoints now require `Authorization: Bearer sk-symbiote_...` header
- Symbiote creation via API sets `owner_id` from authenticated tenant
- `GET /symbiotes/{id}` enforces tenant ownership check
- `POST /sessions/{id}/chat` verifies session belongs to authenticated tenant

## [0.1.7] — 2026-03-17

### Added — Security & Quality (nanobot report)

- [B-14] SSRF Protection — validate URLs against private/internal IP ranges before HTTP requests, redirect validation (`security/network.py`)
- [B-15] Untrusted Content Banner — wrap external HTTP responses with `[External content]` banner to mitigate prompt injection (`environment/tools.py`)
- [B-17] GenerationSettings — configurable temperature/max_tokens/top_p/reasoning_effort with pass-through to LLM (`core/generation.py`)

### Changed

- [B-16] Memory Consolidation — async mode with background thread, sync fallback for SQLite, `_persist_facts()` extracted (`memory/consolidator.py`)
- [B-18] WorkingMemory trim — aligns to user turn boundaries, prevents orphaned assistant messages (`memory/working.py`)
- [B-14] HTTP tool handler uses custom redirect handler that re-validates each redirect URL
- [B-15] HTTP responses (string, dict, list) all wrapped with untrusted content banner

## [0.1.6] — 2026-03-17

### Fixed

- `ForgeLLMAdapter` — use `response.content` instead of `response.message` (forge-llm 0.7.8 API change)
- `ForgeLLMAdapter` — auto-resolve `{PROVIDER}_API_KEY` and `{PROVIDER}_BASE_URL` from env vars when not passed explicitly
- E2E tests default provider changed from `anthropic` to `symgateway`

## [0.1.5] — 2026-03-17

### Added — Nanobot-inspired Architecture

- [B-8] Tool Error Hints — auto-inject retry hints on failed tool calls (`environment/tools.py`)
- [B-9] Runtime Context Strip — ephemeral metadata in LLM prompts without polluting session history (`environment/runtime_context.py`)
- [B-10] Memory Consolidation — LLM-based summarization of working memory when tokens exceed threshold (`memory/consolidator.py`)
- [B-11] Subagent Spawning — inter-Symbiota task delegation with recursion guard and isolated sessions (`runners/subagent.py`)
- [B-12] MessageBus — async inbound/outbound queues for channel decoupling (`bus/`)
- [B-13] Progressive Skills — lazy-loaded .md skills with XML summary for system prompts (`skills/loader.py`)

### Added — Original Backlog

- [B-3] MessageRepository port — isolate SQL from ReflectionEngine via MessagePort protocol (`adapters/storage/message_repository.py`)
- [B-4] Semantic Recall Provider — keyword-based memory scoring with tokenization and stop words (`memory/recall.py`)
- [B-6] ProcessEngine Cache Invalidation — TTL-based cache with `invalidate_cache()` for multi-worker support (`process/engine.py`)
- [B-2] Interactive CLI Chat — REPL loop with `/quit`, `/reflect` commands (`cli/main.py interactive`)
- [B-5] LLM E2E Integration Tests — 5 skipable tests for real LLM validation (`tests/e2e/test_e2e_llm_integration.py`)
- [B-1] Docker Container — multi-stage Dockerfile with health check endpoint, volume persistence

### Changed

- `ReflectionEngine` now depends on `MessagePort` instead of `StoragePort` (B-3)
- `ChatRunner` now injects runtime context and supports optional `MemoryConsolidator` (B-9, B-10)
- `ProcessEngine` constructor accepts `cache_ttl` parameter (B-6)
- `ToolGateway.execute_tool_calls()` appends retry hint to error messages (B-8)
- `SymbioteKernel` now creates `MessageRepository`, `SubagentManager`, registers spawn tool (B-3, B-11)
- HTTP API: added `GET /health` endpoint (B-1)

## [0.1.4] — 2026-03-16

### Added

- symbiote-ui reusable chat Web Component
- Deployment architectures section to QUICKSTART (embedded vs HTTP)

## [MVP] — 2026-03-16

### Added

- [US-01] Identity & Persona — create, persist, update symbiote identity with audit trail
- [US-02] Session Lifecycle — start, resume, close sessions with messages, decisions, summary
- [US-03] Workspace & Workdir — persistent workspaces with artifact tracking on real filesystem
- [US-04] Environment — configurable tools, services, policies per symbiote/workspace
- [US-05] Knowledge Layer — knowledge sources separate from relational memory
- [US-06] Memory Stack — 4 layers: working, session, long-term relational, semantic recall interface
- [US-07] Context Assembly — selective pipeline with configurable token budget
- [US-08] Runners — ChatRunner, ProcessRunner with registry and intent selection
- [US-09] Tools & Policy Gate — fs_read/fs_write/fs_list with deny-by-default authorization and audit log
- [US-10] Process Engine — declarative processes with 5 default definitions and step-by-step execution
- [US-11] 6 Capabilities — Learn, Teach, Chat, Work, Show, Reflect as explicit operations
- [US-12] Reflection Engine — keyword heuristic fact extraction, noise detection, summary generation
- [US-13] Export Service — sessions, memories, decisions as Markdown
- [US-14] Three interfaces — Python library, CLI (Typer+Rich), HTTP API (FastAPI)
- MockLLMAdapter for testing without API key
- ForgeLLMAdapter for Anthropic/OpenAI/OpenRouter via ForgeLLM
- Domain exception hierarchy (EntityNotFoundError, ValidationError, CapabilityError, LLMError)
- 393 tests, ~96% coverage
