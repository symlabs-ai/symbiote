"""ProcessRunner — runner that orchestrates process execution."""

from __future__ import annotations

from symbiote.core.context import AssembledContext
from symbiote.process.engine import ProcessEngine
from symbiote.runners.base import RunResult


class ProcessRunner:
    """Runner that selects and executes a process through all its steps."""

    runner_type: str = "process"

    def __init__(self, engine: ProcessEngine) -> None:
        self._engine = engine

    def can_handle(self, intent: str) -> bool:
        """True if the engine has a matching process definition."""
        return self._engine.select(intent) is not None

    def run(self, context: AssembledContext) -> RunResult:
        """Select process, start instance, advance through all steps."""
        intent = context.user_input
        defn = self._engine.select(intent)
        if defn is None:
            return RunResult(
                success=False,
                error=f"No process definition matches intent: {intent!r}",
                runner_type=self.runner_type,
            )

        instance = self._engine.start(context.session_id, defn.name)

        # Advance through all steps until completed
        while instance.state == "running":
            instance = self._engine.advance(instance.id)

        return RunResult(
            success=True,
            output={
                "instance_id": instance.id,
                "process_name": instance.process_name,
                "state": instance.state,
                "logs": instance.logs,
            },
            runner_type=self.runner_type,
        )
