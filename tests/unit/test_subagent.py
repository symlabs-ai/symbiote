"""Tests for SubagentManager — B-11."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel
from symbiote.core.ports import LLMPort
from symbiote.runners.subagent import SPAWN_DESCRIPTOR, SubagentManager


class MockLLM:
    """Mock LLM that returns a configurable response."""

    def __init__(self, response: str = "I completed the task.") -> None:
        self._response = response
        self.calls: list[list[dict]] = []

    def complete(self, messages: list[dict], config: dict | None = None) -> str:
        self.calls.append(messages)
        return self._response


@pytest.fixture()
def kernel(tmp_path: Path) -> SymbioteKernel:
    config = KernelConfig(db_path=tmp_path / "subagent_test.db")
    llm = MockLLM()
    k = SymbioteKernel(config, llm=llm)
    yield k
    k.shutdown()


class TestSpawnDescriptor:
    def test_descriptor_is_builtin(self) -> None:
        assert SPAWN_DESCRIPTOR.handler_type == "builtin"
        assert SPAWN_DESCRIPTOR.tool_id == "spawn"

    def test_required_params(self) -> None:
        required = SPAWN_DESCRIPTOR.parameters.get("required", [])
        assert "target_symbiote" in required
        assert "task" in required


class TestSubagentRegistration:
    def test_spawn_tool_registered_in_kernel(self, kernel: SymbioteKernel) -> None:
        assert kernel.tool_gateway.has_tool("spawn")

    def test_spawn_descriptor_available(self, kernel: SymbioteKernel) -> None:
        desc = kernel.tool_gateway.get_descriptor("spawn")
        assert desc is not None
        assert desc.tool_id == "spawn"


class TestSpawnExecution:
    def test_spawn_to_existing_symbiote(self, kernel: SymbioteKernel) -> None:
        # Create target Symbiota
        kernel.create_symbiote(name="Helper", role="assistant")

        # Enable spawn tool for caller
        caller = kernel.create_symbiote(name="Caller", role="coordinator")
        kernel.environment.configure(
            symbiote_id=caller.id, tools=["spawn"]
        )

        mgr = SubagentManager(kernel)
        result = mgr.spawn({
            "target_symbiote": "Helper",
            "task": "Summarize the architecture",
        })

        assert result["success"] is True
        assert result["target_symbiote"] == "Helper"
        assert result["response"] is not None
        assert result["session_id"] is not None

    def test_spawn_to_nonexistent_symbiote(self, kernel: SymbioteKernel) -> None:
        mgr = SubagentManager(kernel)
        result = mgr.spawn({
            "target_symbiote": "NonExistent",
            "task": "Do something",
        })

        assert result["success"] is False
        assert "not found" in result["error"]

    def test_spawn_missing_target(self, kernel: SymbioteKernel) -> None:
        mgr = SubagentManager(kernel)
        result = mgr.spawn({"task": "Do something"})

        assert result["success"] is False
        assert "required" in result["error"]

    def test_spawn_missing_task(self, kernel: SymbioteKernel) -> None:
        mgr = SubagentManager(kernel)
        result = mgr.spawn({"target_symbiote": "Helper"})

        assert result["success"] is False
        assert "required" in result["error"]

    def test_spawn_by_symbiote_id(self, kernel: SymbioteKernel) -> None:
        target = kernel.create_symbiote(name="Worker", role="assistant")

        mgr = SubagentManager(kernel)
        result = mgr.spawn({
            "target_symbiote": target.id,
            "task": "Process data",
        })

        assert result["success"] is True

    def test_spawn_creates_isolated_session(self, kernel: SymbioteKernel) -> None:
        kernel.create_symbiote(name="Isolated", role="assistant")

        mgr = SubagentManager(kernel)
        result = mgr.spawn({
            "target_symbiote": "Isolated",
            "task": "Run analysis",
        })

        assert result["success"] is True
        session_id = result["session_id"]

        # Session should be closed after spawn completes
        session = kernel._sessions.resume(session_id)
        # resume reopens, but summary should exist from close
        assert session is not None

    def test_recursion_guard_blocks_deep_nesting(self, kernel: SymbioteKernel) -> None:
        kernel.create_symbiote(name="Recursive", role="assistant")

        mgr = SubagentManager(kernel)
        mgr._depth = SubagentManager.MAX_DEPTH  # simulate deep nesting

        result = mgr.spawn({
            "target_symbiote": "Recursive",
            "task": "Do work",
        })

        assert result["success"] is False
        assert "depth" in result["error"].lower()

    def test_depth_resets_after_spawn(self, kernel: SymbioteKernel) -> None:
        kernel.create_symbiote(name="DepthBot", role="assistant")

        mgr = SubagentManager(kernel)
        assert mgr._depth == 0

        mgr.spawn({
            "target_symbiote": "DepthBot",
            "task": "Quick task",
        })

        assert mgr._depth == 0  # reset after completion

    def test_spawn_session_has_subagent_goal(self, kernel: SymbioteKernel) -> None:
        kernel.create_symbiote(name="GoalBot", role="assistant")

        mgr = SubagentManager(kernel)
        result = mgr.spawn({
            "target_symbiote": "GoalBot",
            "task": "Check status",
        })

        assert result["success"] is True
        # The session goal should indicate it's a subagent task
        row = kernel._storage.fetch_one(
            "SELECT goal FROM sessions WHERE id = ?",
            (result["session_id"],),
        )
        assert "[subagent]" in row["goal"]
