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


def compute_auto_score(
    trace: LoopTrace | None,
    tool_mode: str = "brief",
    has_tools: bool = False,
) -> float:
    """Compute an automatic quality score from a LoopTrace.

    Returns 0.0–1.0 where 1.0 = perfect first-try completion and
    0.0 = exhausted all iterations without completing.

    Scoring is mode-aware:
    - **instant**: No iteration penalty (always 1 iter). Signals: did it
      use a tool when tools were available? Did the tool succeed?
    - **brief** (default): Standard iteration + stop_reason + failure scoring.
    - **continuous**: Relaxed iteration penalty (many iterations expected).
    """
    if trace is None:
        # No loop = direct response.  In instant with tools available,
        # not calling a tool MAY indicate the LLM didn't understand it
        # should act — slightly lower score than a clean tool call.
        if tool_mode == "instant" and has_tools:
            return 0.7
        return 0.8

    # ── Instant mode scoring ──────────────────────────────────────────
    if tool_mode == "instant":
        return _score_instant(trace, has_tools)

    # ── Long-run mode scoring ─────────────────────────────────────────
    if tool_mode == "long_run":
        return _score_long_run(trace)

    # ── Brief / continuous scoring ────────────────────────────────────
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
        if tool_mode == "continuous":
            # Continuous: relaxed — many iterations are expected
            if trace.total_iterations <= 5:
                iter_factor = 1.0
            elif trace.total_iterations <= 10:
                iter_factor = 0.8
            else:
                iter_factor = 0.6
        else:
            # Brief: calibrated for multi-step tasks (3-10 iterations
            # are normal for "list + email + WhatsApp" type tasks)
            if trace.total_iterations <= 3:
                iter_factor = 1.0
            elif trace.total_iterations <= 7:
                iter_factor = 0.85
            elif trace.total_iterations <= 10:
                iter_factor = 0.7
            else:
                iter_factor = 0.5
        base *= iter_factor

    # Penalize by tool failure rate
    if trace.steps:
        failure_count = sum(1 for s in trace.steps if not s.success)
        failure_rate = failure_count / len(trace.steps)
        base *= 1 - failure_rate * 0.3

    return round(max(0.0, min(1.0, base)), 2)


def _score_instant(trace: LoopTrace, has_tools: bool) -> float:
    """Scoring logic specific to instant mode.

    In instant mode there is at most 1 LLM call and 0-1 tool calls.
    Iteration count is irrelevant.  The signals that matter are:
    1. Did the tool call succeed (if any)?
    2. Did the LLM use a tool when tools were available?
    """
    # No tool calls at all
    if not trace.steps:
        # If tools were available but not used, slightly lower —
        # the LLM may have answered directly (fine) or missed the intent
        return 0.7 if has_tools else 0.9

    # Exactly 1 tool call (typical for instant)
    step = trace.steps[0]
    if step.success:
        return 1.0  # used tool, succeeded — perfect instant

    # Tool call failed
    return 0.3  # single chance, failed — significant penalty


def _score_long_run(trace: LoopTrace) -> float:
    """Scoring logic specific to long-run mode.

    Long-run tasks are projects — many iterations are expected and normal.
    The signal that matters is completion rate (blocks done / total) and
    block failure rate, NOT iteration count.

    stop_reason mapping:
    - end_turn: all blocks completed = 1.0
    - block_failure: partial completion, score by ratio
    - stagnation/circuit_breaker: underlying tool issues = low score
    """
    reason_scores = {
        "end_turn": 1.0,
        "block_failure": 0.6,  # partial completion is still valuable
        "stagnation": 0.2,
        "circuit_breaker": 0.1,
        "max_iterations": 0.3,  # may have completed many blocks
        "timeout": 0.3,
    }
    base = reason_scores.get(trace.stop_reason or "end_turn", 0.5)

    # Penalize by block failure rate (from steps where tool_id starts with "block:")
    block_steps = [s for s in trace.steps if s.tool_id.startswith("block:")]
    if block_steps:
        success_rate = sum(1 for s in block_steps if s.success) / len(block_steps)
        base *= (0.4 + 0.6 * success_rate)  # 40% floor + 60% from success

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
