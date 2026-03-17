"""SubagentManager — delegate tasks between Symbiotas with restricted tool sets."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from symbiote.environment.descriptors import ToolDescriptor

if TYPE_CHECKING:
    from symbiote.core.kernel import SymbioteKernel

# ── Models ────────────────────────────────────────────────────────────────────


class SpawnResult(BaseModel):
    """Result of a subagent spawn."""

    success: bool
    target_symbiote: str
    task: str
    response: str | None = None
    error: str | None = None
    session_id: str | None = None


# ── Tool descriptor ──────────────────────────────────────────────────────────

SPAWN_DESCRIPTOR = ToolDescriptor(
    tool_id="spawn",
    name="Spawn Subagent",
    description=(
        "Delegate a task to another Symbiota. The target Symbiota will process "
        "the task in an isolated session and return the result. "
        "Use this for tasks outside your expertise or for parallel work."
    ),
    parameters={
        "type": "object",
        "properties": {
            "target_symbiote": {
                "type": "string",
                "description": "Name or ID of the target Symbiota to delegate to",
            },
            "task": {
                "type": "string",
                "description": "The task description for the target Symbiota",
            },
        },
        "required": ["target_symbiote", "task"],
    },
    handler_type="builtin",
)


# ── Restricted tools ─────────────────────────────────────────────────────────

# Subagents can NOT use these tools (prevent recursion and side effects)
_BLOCKED_TOOLS = frozenset({"spawn", "message"})


# ── SubagentManager ──────────────────────────────────────────────────────────


class SubagentManager:
    """Manages task delegation between Symbiotas.

    When a Symbiota calls the ``spawn`` tool, this manager:
    1. Resolves the target Symbiota (by name or ID)
    2. Creates an isolated session on the target
    3. Runs the task via the kernel's message flow
    4. Returns the result to the caller

    Recursion guard: max_depth prevents infinite A→B→A loops.
    """

    MAX_DEPTH = 3

    def __init__(self, kernel: SymbioteKernel) -> None:
        self._kernel = kernel
        self._depth = 0

    def spawn(self, params: dict) -> dict[str, Any]:
        """Execute a spawn tool call.

        Args:
            params: {"target_symbiote": str, "task": str}

        Returns:
            Dict with spawn result details.
        """
        target_name = params.get("target_symbiote", "")
        task = params.get("task", "")

        # Recursion guard
        if self._depth >= self.MAX_DEPTH:
            return SpawnResult(
                success=False,
                target_symbiote=target_name,
                task=task,
                error=f"Max spawn depth ({self.MAX_DEPTH}) exceeded — cannot delegate further",
            ).model_dump()

        if not target_name:
            return SpawnResult(
                success=False,
                target_symbiote=target_name,
                task=task,
                error="target_symbiote is required",
            ).model_dump()

        if not task:
            return SpawnResult(
                success=False,
                target_symbiote=target_name,
                task=task,
                error="task is required",
            ).model_dump()

        # Resolve target Symbiota
        target = self._resolve_symbiote(target_name)
        if target is None:
            return SpawnResult(
                success=False,
                target_symbiote=target_name,
                task=task,
                error=f"Symbiota '{target_name}' not found",
            ).model_dump()

        # Create isolated session (with depth tracking)
        self._depth += 1
        try:
            session = self._kernel.start_session(
                symbiote_id=target.id,
                goal=f"[subagent] {task}",
            )

            # Run the task
            response = self._kernel.message(
                session_id=session.id,
                content=task,
            )

            # Close the session
            self._kernel.close_session(session.id)

            # Normalize response
            if isinstance(response, dict):
                response_text = response.get("text", str(response))
            else:
                response_text = str(response)

            return SpawnResult(
                success=True,
                target_symbiote=target_name,
                task=task,
                response=response_text,
                session_id=session.id,
            ).model_dump()

        except Exception as exc:
            return SpawnResult(
                success=False,
                target_symbiote=target_name,
                task=task,
                error=f"{type(exc).__name__}: {exc}",
            ).model_dump()
        finally:
            self._depth -= 1

    def _resolve_symbiote(self, name_or_id: str):
        """Resolve a Symbiota by name or ID."""
        # Try by ID first
        sym = self._kernel.get_symbiote(name_or_id)
        if sym is not None:
            return sym

        # Try by name via kernel's public method
        return self._kernel.find_symbiote_by_name(name_or_id)

        return None

    def register(self) -> None:
        """Register the spawn tool in the kernel's ToolGateway."""
        self._kernel.tool_gateway.register_descriptor(
            SPAWN_DESCRIPTOR, self.spawn
        )
