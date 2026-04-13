"""Dream Mode data models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from symbiote.core.ports import LLMPort, StoragePort
    from symbiote.memory.store import MemoryStore


def _uuid() -> str:
    return str(uuid4())


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


# ── Budget tracker ──────────────────────────────────────────────────────────


class BudgetTracker:
    """Tracks LLM call budget within a single dream cycle."""

    def __init__(self, max_calls: int) -> None:
        self._max = max_calls
        self._used = 0

    def consume(self, n: int = 1) -> bool:
        """Try to consume *n* calls. Returns False if budget exhausted."""
        if self._used + n > self._max:
            return False
        self._used += n
        return True

    @property
    def remaining(self) -> int:
        return max(0, self._max - self._used)

    @property
    def used(self) -> int:
        return self._used


# ── Dream context (passed to each phase) ────────────────────────────────────


@dataclass
class DreamContext:
    symbiote_id: str
    storage: StoragePort
    memory: MemoryStore
    llm: LLMPort | None
    budget: BudgetTracker
    dry_run: bool
    last_dream_at: datetime | None = None


# ── Phase result ────────────────────────────────────────────────────────────


class DreamPhaseResult(BaseModel):
    phase: str
    started_at: datetime = Field(default_factory=_utcnow)
    completed_at: datetime = Field(default_factory=_utcnow)
    actions_proposed: int = 0
    actions_applied: int = 0
    llm_calls_used: int = 0
    details: list[dict] = Field(default_factory=list)
    error: str | None = None


# ── Dream report ────────────────────────────────────────────────────────────


class DreamReport(BaseModel):
    id: str = Field(default_factory=_uuid)
    symbiote_id: str
    started_at: datetime = Field(default_factory=_utcnow)
    completed_at: datetime | None = None
    dream_mode: Literal["light", "full"] = "light"
    dry_run: bool = False
    total_llm_calls: int = 0
    max_llm_calls: int = 10
    phases: list[DreamPhaseResult] = Field(default_factory=list)
