"""What a turn hands back to the host.

``message()`` / ``message_async()`` return the reply text, or a
:class:`TurnReply` when the turn called tools. ``turn()`` / ``turn_async()``
always return a :class:`TurnOutcome`: the same reply plus why the tool loop
stopped, without reading ``execution_traces``.
"""

from __future__ import annotations

from typing import Any, TypedDict

from pydantic import BaseModel, Field

from symbiote.environment.descriptors import ToolCallResult
from symbiote.runners.base import LoopTrace

# Why a turn that returned an answer stopped looping. Only "end_turn" means
# the model finished on its own; the others ended the loop early (see
# "What a turn returns, and why it stopped" in docs/HOST_INTEGRATION.md for
# where the text comes from in each case).
STOP_REASONS: dict[str, str] = {
    "end_turn": "the model answered without asking for more tools",
    "max_iterations": "the turn reached max_tool_iterations",
    "stagnation": "the same successful call was repeated",
    "circuit_breaker": "a tool failed three times in a row",
    "timeout": "the loop ran past its time limit",
    "budget": "the turn reached its spending ceiling (max_turn_tokens / max_turn_llm_calls)",
    "on_after_tool_result": "the host's on_after_tool_result hook ended the turn",
    "block_failure": "a long_run project ended with blocks not completed",
}


class TurnReply(TypedDict):
    """What ``message()`` returns when the turn called tools."""

    text: str
    tool_results: list[dict[str, Any]]


class TurnUsage(BaseModel):
    """What the model calls made to answer a turn cost.

    Counts every call of the turn, the ones of a symbiote it delegated to
    (``spawn``) included. Token counts are sums of what the LLM adapter
    reported: they stay at 0 for an adapter that reports none, while
    ``llm_calls`` is always counted. ``prompt_tokens`` is the whole input;
    ``cache_read_tokens`` and ``cache_write_tokens`` are the parts of it read
    from and written to the provider's prompt cache.
    """

    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class TurnOutcome(BaseModel):
    """A finished turn: the reply and how the tool loop ended."""

    text: str
    tool_results: list[ToolCallResult] = Field(default_factory=list)
    # One of STOP_REASONS. A turn that answered without the tool loop
    # (no tool call at all) reports "end_turn" with zero iterations.
    stop_reason: str = "end_turn"
    iterations: int = 0
    tool_calls: int = 0
    elapsed_ms: int = 0
    tool_mode: str | None = None
    usage: TurnUsage = Field(default_factory=TurnUsage)

    @property
    def stopped_early(self) -> bool:
        """True when the loop was ended by a limit, not by the model."""
        return self.stop_reason != "end_turn"

    @property
    def pending_approvals(self) -> list[str]:
        """Ids of the tool calls this turn left waiting for a person's
        approval (``ApprovalDecision.defer``): see
        ``kernel.inspector.get_pending`` and ``kernel.resolve_pending``."""
        return [r.pending_approval_id for r in self.tool_results if r.pending_approval_id]


def turn_outcome(
    response: str | TurnReply | Any,
    trace: LoopTrace | None,
    usage: dict[str, int] | None = None,
) -> TurnOutcome:
    """Build the outcome of a turn from what ``message()`` would return, the
    loop trace of that same turn and its token totals."""
    turn_usage = TurnUsage(**usage) if usage else TurnUsage()
    if isinstance(response, dict):
        text = str(response.get("text") or "")
        results = [ToolCallResult.model_validate(r) for r in response.get("tool_results") or []]
    else:
        text = "" if response is None else str(response)
        results = []
    if trace is None:
        return TurnOutcome(text=text, tool_results=results, usage=turn_usage)
    return TurnOutcome(
        text=text,
        tool_results=results,
        usage=turn_usage,
        stop_reason=trace.stop_reason or "end_turn",
        iterations=trace.total_iterations,
        tool_calls=trace.total_tool_calls,
        elapsed_ms=trace.total_elapsed_ms,
        tool_mode=trace.tool_mode,
    )
