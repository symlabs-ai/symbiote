# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/)

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
