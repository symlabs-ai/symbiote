"""Runner base protocol, RunResult model, RunnerRegistry, and EchoRunner."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from symbiote.core.context import AssembledContext

# ── RunResult model ──────────────────────────────────────────────────────────


class LoopStep(BaseModel):
    """One iteration of the tool loop."""

    iteration: int
    tool_id: str
    params: dict[str, Any] = {}
    success: bool = False
    error: str | None = None
    elapsed_ms: int = 0


class LoopTrace(BaseModel):
    """Full trace of a tool-loop execution."""

    steps: list[LoopStep] = []
    total_iterations: int = 0
    total_tool_calls: int = 0
    total_elapsed_ms: int = 0


class RunResult(BaseModel):
    """Standardised result returned by every runner."""

    success: bool
    output: Any = None
    error: str | None = None
    runner_type: str = ""
    loop_trace: LoopTrace | None = None


# ── Runner protocol ─────────────────────────────────────────────────────────


@runtime_checkable
class Runner(Protocol):
    """Structural interface every runner must satisfy."""

    runner_type: str

    def can_handle(self, intent: str) -> bool: ...

    def run(self, context: AssembledContext) -> RunResult: ...


# ── Registry ─────────────────────────────────────────────────────────────────


class RunnerRegistry:
    """Holds registered runners and selects the right one for a given intent."""

    def __init__(self) -> None:
        self._runners: list[Runner] = []

    def register(self, runner: Runner) -> None:
        """Add a runner to the registry."""
        self._runners.append(runner)

    def select(self, intent: str) -> Runner | None:
        """Return the first runner that can handle *intent*, or None."""
        for runner in self._runners:
            if runner.can_handle(intent):
                return runner
        return None

    def list_runners(self) -> list[str]:
        """Return the runner_type name of every registered runner."""
        return [r.runner_type for r in self._runners]


# ── EchoRunner (useful for testing) ─────────────────────────────────────────


class EchoRunner:
    """Trivial runner that echoes the user input back."""

    runner_type: str = "echo"

    def can_handle(self, intent: str) -> bool:
        return intent == "echo"

    def run(self, context: AssembledContext) -> RunResult:
        return RunResult(
            success=True,
            output=context.user_input,
            runner_type=self.runner_type,
        )
