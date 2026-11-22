"""Kernel global configuration — Pydantic v2 model."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class KernelConfig(BaseModel):
    """Global configuration for the Symbiote kernel.

    Works with zero configuration — every field has a sensible default.
    """

    db_path: Path = Path(".symbiote/symbiote.db")
    context_budget: int = 4000
    # Per-result ceiling (chars) for tool outputs fed back to the LLM in the
    # chat tool loop (Layer 1 microcompaction). Hosts whose tools return large
    # JSON payloads (e.g. full task trees) should raise this so results are
    # not cut mid-payload — the LLM treats the missing tail as nonexistent.
    # Raise loop_context_budget with it: a result larger than about half
    # the loop budget is truncated again by autocompact.
    tool_result_max_chars: int = 3000
    # Token budget for the chat tool loop (system prompt + history + loop
    # messages). Past 80% of it, older loop steps are summarized; the most
    # recent tool results are kept (truncated if they alone overflow).
    loop_context_budget: int = 16_000
    # After this many tool rounds in a turn, every round replaces the loop's
    # older exchanges with one line per step ("1) read_file → ...") and keeps
    # only the latest exchange in full. None or 0 turns this off: older steps
    # stay in full until the turn passes 80% of loop_context_budget. Hosts
    # whose model reads files over many rounds (a coding agent) turn it off
    # and raise loop_context_budget instead.
    loop_compaction_rounds: int | None = Field(default=4, ge=0)
    # What the kernel prepends to the user message it sends to the model (it
    # is never persisted): "full" = timestamp and session id (the historical
    # behaviour), "timestamp" = no session id, "off" = nothing.
    runtime_context: Literal["full", "timestamp", "off"] = "full"
    llm_provider: str = "forge"
    agent_protocol: str = "legacy_text_v1"
    log_level: str = "INFO"
    # Optional skill library roots. If None, the kernel derives
    # ``{db_path.parent}/skills/agent`` as the agent write root and (if it
    # exists) ``./skills`` next to the project as a read+modify root. Host
    # apps can override to point at any layout (multi-tenant, shared, etc.).
    skills_root: Path | None = None
    skills_extra_roots: list[Path] = []
    skills_protected_roots: list[Path] = []
    # How the skill library is partitioned across symbiotes:
    #   "global"       — ONE SkillsStore/SkillsLoader for the whole kernel,
    #                    rooted at skills_root (legacy/default behaviour). All
    #                    symbiotes share one pool. Correct for single-symbiote
    #                    hosts (CLI) and trusted multi-symbiote setups.
    #   "per_symbiote" — each symbiote gets its OWN read-write root at
    #                    {skills_root}/<symbiote_id>/skills/... plus the shared
    #                    read-only catalogue in skills_protected_roots. No
    #                    symbiote can see or write another's skills. Required
    #                    for multi-tenant hosts (one kernel, N users). Opt-in.
    skill_scope: Literal["global", "per_symbiote"] = "global"
    # Host-controlled ceiling for per-symbiote ``max_tool_iterations``. The
    # embedding application owns this policy; ``EnvironmentManager.configure()``
    # rejects per-symbiote values above it. Default 50 preserves the historical
    # hardcoded cap. Bounded by the EnvironmentConfig absolute backstop (10000).
    max_tool_iterations_ceiling: int = 50
    # Optional retention (off by default): ``kernel.prune()`` deletes the
    # messages / execution traces of sessions closed more than N days ago.
    # Nothing runs it automatically — call it from a cron (``symbiote prune``).
    message_retention_days: int | None = Field(default=None, ge=0)
    trace_retention_days: int | None = Field(default=None, ge=0)
    # Same for long-term memory: ``kernel.prune()`` deletes the memory entries
    # created more than N days ago, whatever their session.
    memory_retention_days: int | None = Field(default=None, ge=0)
    # Same for the record of model calls (``llm_usage``): ``kernel.prune()``
    # deletes the rows older than N days, whatever their session.
    usage_retention_days: int | None = Field(default=None, ge=0)
    # The summary of the sessions closed more than N days ago is cleared. The
    # default summary is the text of the session's last messages: a host that
    # removes messages for privacy sets this window too.
    summary_retention_days: int | None = Field(default=None, ge=0)
    # The audit rows (``audit_log`` with the arguments of every tool call,
    # ``reflection_audit``, ``skill_review_audit``) created more than N days
    # ago, and the approval requests resolved more than N days ago.
    audit_retention_days: int | None = Field(default=None, ge=0)
    # The sessions closed more than N days ago are deleted with everything
    # tied to them. Memory entries stay, under ``memory_retention_days``.
    session_retention_days: int | None = Field(default=None, ge=0)
    # What the message and trace windows measure. "closed": sessions closed
    # more than N days ago (a session never closed keeps everything). "age":
    # also every message and trace created more than N days ago, in any
    # session, the active ones included.
    retention_basis: Literal["closed", "age"] = "closed"
    # How long a tool call deferred by the approval callback waits for a
    # person before it expires (``ApprovalDecision.defer(expires_in=...)``
    # replaces it for one request). ``None``: it waits for ever.
    pending_approval_ttl_hours: float | None = Field(default=168.0, gt=0)
    # Preferences and constraints enter every turn's context (the most
    # important first, up to this many), whatever words the message uses:
    # "prefers short answers" applies to a question about the week too.
    # The other memories still come by relevance. 0 = relevance only.
    standing_memory_limit: int = Field(default=5, ge=0)
    # Kill switch for everything that writes skills (the ``skill_manage``
    # tool, the background review, dream's lifecycle changes, usage counters).
    # False makes the skill library read-only for the whole kernel: for hosts
    # whose replicas do not share a disk.
    skill_writes: bool = True
    # ``kernel.cross_learning_candidates()`` reads the harness versions of
    # OTHER symbiotes. False makes it return nothing: for hosts where each
    # symbiote belongs to a different user.
    cross_symbiote_learning: bool = True
    # How many per-symbiote objects (skill loader/store pairs, background
    # review engines) stay cached. Least recently used ones are dropped and
    # rebuilt on demand, so memory does not grow with the number of symbiotes.
    symbiote_cache_size: int = Field(default=256, ge=1)
    # Session lock (one turn at a time per session; see SessionLockPort).
    # ``session_lock_wait``: seconds a turn waits for the lock before
    # ``SessionBusyError``; None waits forever (the historical behaviour).
    session_lock_wait: float | None = Field(default=None, ge=0)
    # ``session_lock_ttl``: validity of a held lock, in seconds; None uses the
    # lock implementation's default (the in-process lock never expires).
    session_lock_ttl: float | None = Field(default=None, gt=0)
    # ``session_lock_renew_interval``: while a turn runs, the lock is renewed
    # every N seconds; a turn whose lock cannot be renewed stops before its
    # next step. None renews at a third of the ttl (no renewal without ttl).
    session_lock_renew_interval: float | None = Field(default=None, gt=0)
    # How often a running turn asks its session lock whether another replica
    # requested its cancellation (``kernel.cancel_turn`` there). Only locks
    # that relay cancellations are asked (``StorageSessionLock``).
    turn_cancel_poll_interval: float = Field(default=1.0, gt=0)
    # Root directory for the builtin filesystem tools (fs_read / fs_write /
    # fs_list) and the default working directory of ``bash``. When set, every
    # path must resolve (symlinks included) inside it and an ``allowed_root``
    # passed in the tool params can only narrow it. ``None`` keeps the legacy
    # unrestricted behaviour and logs a warning on first use. ``bash`` is not a
    # sandbox: the root only fixes its starting directory.
    fs_allowed_root: Path | None = None

    # ── validators ────────────────────────────────────────────────────

    @field_validator("context_budget")
    @classmethod
    def _context_budget_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("context_budget must be a positive integer")
        return v

    @field_validator("tool_result_max_chars")
    @classmethod
    def _tool_result_max_chars_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("tool_result_max_chars must be a positive integer")
        return v

    @field_validator("max_tool_iterations_ceiling")
    @classmethod
    def _ceiling_in_range(cls, v: int) -> int:
        if not 1 <= v <= 10000:
            raise ValueError(
                "max_tool_iterations_ceiling must be between 1 and 10000 "
                f"(the EnvironmentConfig absolute backstop), got {v}"
            )
        return v

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in _VALID_LOG_LEVELS:
            raise ValueError(
                f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}, got {v!r}"
            )
        return v

    @field_validator("llm_provider")
    @classmethod
    def _strip_provider(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("llm_provider must not be empty")
        return v

    @field_validator("agent_protocol")
    @classmethod
    def _strip_agent_protocol(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("agent_protocol must not be empty")
        return v

    @model_validator(mode="after")
    def _renew_before_expiry(self) -> Self:
        ttl, interval = self.session_lock_ttl, self.session_lock_renew_interval
        if ttl is not None and interval is not None and interval >= ttl:
            raise ValueError(
                "session_lock_renew_interval must be shorter than session_lock_ttl"
            )
        return self

    # ── class methods ─────────────────────────────────────────────────

    @classmethod
    def from_yaml(cls, path: str | Path) -> Self:
        """Load configuration from a YAML file.

        Missing keys fall back to defaults.  An empty file yields all defaults.
        Raises ``FileNotFoundError`` if *path* does not exist.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        with path.open() as fh:
            data = yaml.safe_load(fh)

        if data is None:
            data = {}

        return cls(**data)
