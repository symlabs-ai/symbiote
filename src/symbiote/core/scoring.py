"""SessionScore — automatic feedback signal from LoopTrace data.

Computes a 0.0–1.0 score for each session based on how the tool loop
performed.  This score is the foundation for harness self-evolution:
it tells the system whether a session "went well" without requiring
explicit user feedback.

Three signal layers compose the score:
  1. stop_reason (did it finish or crash?)
  2. iteration efficiency (how many tries?)
  3. tool failure rate (how many tools errored?)

An optional user_score from the host enriches the signal when available.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, Field

from symbiote.runners.base import LoopTrace


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _uuid() -> str:
    return str(uuid4())


class SessionScore(BaseModel):
    """Persisted score for a session's tool-loop execution."""

    id: str = Field(default_factory=_uuid)
    session_id: str
    symbiote_id: str
    auto_score: float = Field(default=0.0, ge=0.0, le=1.0)
    user_score: float | None = None
    final_score: float = Field(default=0.0, ge=0.0, le=1.0)
    stop_reason: str | None = None
    total_iterations: int = 0
    total_tool_calls: int = 0
    computed_at: datetime = Field(default_factory=_utcnow)


def compute_auto_score(trace: LoopTrace | None) -> float:
    """Compute an automatic quality score from a LoopTrace.

    Returns 0.0–1.0 where 1.0 = perfect first-try completion and
    0.0 = exhausted all iterations without completing.
    """
    if trace is None:
        return 0.8  # no loop = direct response, presumably fine

    # Base score by stop_reason
    reason_scores = {
        "end_turn": 1.0,
        "stagnation": 0.2,
        "circuit_breaker": 0.1,
        "max_iterations": 0.0,
    }
    base = reason_scores.get(trace.stop_reason or "end_turn", 0.5)

    # Penalize by iteration count (only if completed successfully)
    if trace.stop_reason == "end_turn" and trace.total_iterations > 0:
        if trace.total_iterations <= 2:
            iter_factor = 1.0
        elif trace.total_iterations <= 4:
            iter_factor = 0.7
        else:
            iter_factor = 0.4
        base *= iter_factor

    # Penalize by tool failure rate
    if trace.steps:
        failure_count = sum(1 for s in trace.steps if not s.success)
        failure_rate = failure_count / len(trace.steps)
        base *= 1 - failure_rate * 0.3

    return round(max(0.0, min(1.0, base)), 2)


def compute_final_score(
    auto_score: float, user_score: float | None = None
) -> float:
    """Compose auto and user scores into a final score.

    When user_score is available: 60% auto + 40% user.
    When not: 100% auto.
    """
    if user_score is not None:
        return round(auto_score * 0.6 + user_score * 0.4, 2)
    return auto_score
