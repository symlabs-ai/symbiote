"""Pydantic v2 domain models for Symbiote entities."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _uuid() -> str:
    return str(uuid4())


# ── Symbiote ─────────────────────────────────────────────────────────────────


class Symbiote(BaseModel):
    id: str = Field(default_factory=_uuid)
    name: str
    role: str
    owner_id: str | None = None
    persona_json: dict = Field(default_factory=dict)
    behavioral_constraints: list[str] = Field(default_factory=list)
    interaction_style: str | None = None
    status: str = "active"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


# ── Session ──────────────────────────────────────────────────────────────────


class Session(BaseModel):
    id: str = Field(default_factory=_uuid)
    symbiote_id: str
    goal: str | None = None
    workspace_id: str | None = None
    external_key: str | None = None
    status: str = "active"
    started_at: datetime = Field(default_factory=_utcnow)
    ended_at: datetime | None = None
    summary: str | None = None


# ── Message ──────────────────────────────────────────────────────────────────


class Message(BaseModel):
    id: str = Field(default_factory=_uuid)
    session_id: str
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: datetime = Field(default_factory=_utcnow)


# ── MemoryEntry ──────────────────────────────────────────────────────────────

# Higher-level category that groups memory types for policy purposes.
# "ephemeral" — short-lived working data, auto-expires
# "declarative" — facts, preferences, constraints (what is/was true)
# "procedural" — how-to knowledge, workflows, conventions
# "meta" — summaries, reflections, notes about other memories
MemoryCategory = Literal["ephemeral", "declarative", "procedural", "meta"]

# Maps each memory type to its category for automatic classification.
MEMORY_TYPE_CATEGORY: dict[str, MemoryCategory] = {
    "working": "ephemeral",
    "session_summary": "meta",
    "relational": "declarative",
    "preference": "declarative",
    "constraint": "declarative",
    "factual": "declarative",
    "procedural": "procedural",
    "decision": "declarative",
    "reflection": "meta",
    "semantic_note": "meta",
}


class MemoryEntry(BaseModel):
    id: str = Field(default_factory=_uuid)
    symbiote_id: str
    session_id: str | None = None
    type: Literal[
        "working",
        "session_summary",
        "relational",
        "preference",
        "constraint",
        "factual",
        "procedural",
        "decision",
        "reflection",
        "semantic_note",
    ]
    category: MemoryCategory | None = None
    scope: Literal["global", "user", "project", "workspace", "session"]
    content: str
    tags: list[str] = Field(default_factory=list)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    source: Literal["user", "system", "reflection", "inference"]
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=_utcnow)
    last_used_at: datetime = Field(default_factory=_utcnow)
    is_active: bool = True

    def model_post_init(self, __context: object) -> None:
        """Auto-classify category from type if not explicitly set."""
        if self.category is None:
            self.category = MEMORY_TYPE_CATEGORY.get(self.type, "declarative")


# ── Workspace ────────────────────────────────────────────────────────────────


class Workspace(BaseModel):
    id: str = Field(default_factory=_uuid)
    symbiote_id: str
    name: str
    root_path: str
    type: Literal["code", "docs", "data", "general"] = "general"
    created_at: datetime = Field(default_factory=_utcnow)


# ── Artifact ─────────────────────────────────────────────────────────────────


class Artifact(BaseModel):
    id: str = Field(default_factory=_uuid)
    session_id: str
    workspace_id: str
    path: str
    type: Literal["file", "directory", "report", "export"]
    description: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


# ── EnvironmentConfig ────────────────────────────────────────────────────────


class EnvironmentConfig(BaseModel):
    id: str = Field(default_factory=_uuid)
    symbiote_id: str
    workspace_id: str | None = None
    tools: list[str] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    humans: list[str] = Field(default_factory=list)
    policies: dict = Field(default_factory=dict)
    resources: dict = Field(default_factory=dict)
    tool_tags: list[str] = Field(default_factory=list)
    tool_loading: Literal["full", "index", "semantic"] = "full"
    tool_mode: Literal["instant", "brief", "long_run", "continuous"] = "brief"
    tool_loop: bool = True  # deprecated — derived from tool_mode
    prompt_caching: bool = False
    memory_share: float = Field(default=0.40, ge=0.0, le=1.0)
    knowledge_share: float = Field(default=0.25, ge=0.0, le=1.0)
    max_tool_iterations: int = Field(default=10, ge=1, le=50)
    tool_call_timeout: float = Field(default=30.0, ge=1.0, le=300.0)
    context_mode: Literal["packed", "on_demand"] = "packed"
    loop_timeout: float = Field(default=300.0, ge=10.0, le=3600.0)
    # Long-run mode configuration
    planner_prompt: str | None = None
    evaluator_prompt: str | None = None
    evaluator_criteria: list[dict] | None = None
    context_strategy: Literal["compaction", "reset", "hybrid"] = "hybrid"
    max_blocks: int = Field(default=20, ge=1, le=100)


# ── Decision ─────────────────────────────────────────────────────────────────


class Decision(BaseModel):
    id: str = Field(default_factory=_uuid)
    session_id: str
    title: str
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)


# ── ProcessInstance ──────────────────────────────────────────────────────────


class ProcessInstance(BaseModel):
    id: str = Field(default_factory=_uuid)
    session_id: str
    process_name: str
    state: Literal["running", "paused", "completed", "failed"]
    current_step: str | None = None
    logs: list[dict] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
