"""Kernel global configuration — Pydantic v2 model."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, field_validator

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
    tool_result_max_chars: int = 2000
    llm_provider: str = "forge"
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
