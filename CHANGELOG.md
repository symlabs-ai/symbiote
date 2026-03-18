# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/)

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
