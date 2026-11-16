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
    "on_after_tool_result": "the host's on_after_tool_result hook ended the turn",
    "block_failure": "a long_run project ended with blocks not completed",
}


class TurnReply(TypedDict):
    """What ``message()`` returns when the turn called tools."""

    text: str
    tool_results: list[dict[str, Any]]


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

    @property
    def stopped_early(self) -> bool:
        """True when the loop was ended by a limit, not by the model."""
        return self.stop_reason != "end_turn"


def turn_outcome(response: str | TurnReply | Any, trace: LoopTrace | None) -> TurnOutcome:
    """Build the outcome of a turn from what ``message()`` would return and
    the loop trace of that same turn."""
    if isinstance(response, dict):
        text = str(response.get("text") or "")
        results = [ToolCallResult.model_validate(r) for r in response.get("tool_results") or []]
    else:
        text = "" if response is None else str(response)
        results = []
    if trace is None:
        return TurnOutcome(text=text, tool_results=results)
    return TurnOutcome(
        text=text,
        tool_results=results,
        stop_reason=trace.stop_reason or "end_turn",
        iterations=trace.total_iterations,
        tool_calls=trace.total_tool_calls,
        elapsed_ms=trace.total_elapsed_ms,
        tool_mode=trace.tool_mode,
    )
