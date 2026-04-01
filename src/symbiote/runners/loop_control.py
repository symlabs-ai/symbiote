"""LoopController — monitors tool loop health and decides when to stop."""

from __future__ import annotations

import json


class LoopController:
    """Monitors tool loop health and decides when to stop.

    Detects three stop conditions:
    1. Max iterations reached
    2. Stagnation: same tool_id + same params called 2+ times consecutively
    3. Circuit breaker: same tool_id failed 3+ times consecutively
    """

    _DEFAULT_STAGNATION_MSG = (
        "You are repeating the same action. "
        "The task may already be complete. Respond to the user."
    )
    _DEFAULT_CIRCUIT_BREAKER_MSG = (
        "Tool '{tool_id}' is unavailable (failed {count} times). "
        "Do not retry it. Respond to the user with what you have."
    )

    def __init__(
        self,
        max_iterations: int = 10,
        stagnation_msg: str | None = None,
        circuit_breaker_msg: str | None = None,
    ) -> None:
        self._max_iterations = max_iterations
        self._stagnation_msg = stagnation_msg or self._DEFAULT_STAGNATION_MSG
        self._circuit_breaker_msg = circuit_breaker_msg or self._DEFAULT_CIRCUIT_BREAKER_MSG
        self._history: list[tuple[str, str, bool]] = []  # (tool_id, params_key, success)
        self._failure_counts: dict[str, int] = {}  # tool_id -> consecutive failures
        self._iteration = 0

    def record(self, tool_id: str, params: dict, success: bool) -> None:
        """Record a tool call result."""
        params_key = json.dumps(params, sort_keys=True, default=str)
        self._history.append((tool_id, params_key, success))
        self._iteration += 1

        # Update consecutive failure tracking
        if success:
            self._failure_counts[tool_id] = 0
        else:
            self._failure_counts[tool_id] = self._failure_counts.get(tool_id, 0) + 1

    def should_stop(self) -> tuple[bool, str]:
        """Return (should_stop, reason) based on loop health.

        Stop conditions:
        1. Max iterations reached
        2. Duplicate detection: same tool_id + same params called 2+ times consecutively
        3. Circuit breaker: same tool_id failed 3+ times consecutively
        """
        if self._iteration >= self._max_iterations:
            return True, "max_iterations"

        # Circuit breaker: any tool failed 3+ times consecutively
        for _tool_id, count in self._failure_counts.items():
            if count >= 3:
                return True, "circuit_breaker"

        # Duplicate detection: last two calls are identical (same tool_id + params)
        if len(self._history) >= 2:
            prev_tool, prev_params, _ = self._history[-2]
            curr_tool, curr_params, _ = self._history[-1]
            if prev_tool == curr_tool and prev_params == curr_params:
                return True, "stagnation"

        return False, ""

    def get_injection_message(self) -> str | None:
        """Return a message to inject into the conversation when stopping.

        Returns None if no injection needed (e.g. max_iterations or no stop).
        """
        should_stop, reason = self.should_stop()
        if not should_stop:
            return None

        if reason == "stagnation":
            return self._stagnation_msg

        if reason == "circuit_breaker":
            # Find the tool that triggered the breaker
            for tool_id, count in self._failure_counts.items():
                if count >= 3:
                    return self._circuit_breaker_msg.format(
                        tool_id=tool_id, count=count,
                    )

        # max_iterations — no special injection needed
        return None
