"""Tests for Prompt Evolution — Fase 3 of Meta-Harness plan (H-09).

Covers:
  H-09.1: Bridge — ContextAssembler resolves overrides, ChatRunner/LoopController use them
  H-09.2: HarnessEvolver — guard rails, evolution flow, rollback
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.context import AssembledContext, ContextAssembler
from symbiote.core.identity import IdentityManager
from symbiote.environment.manager import EnvironmentManager
from symbiote.harness.evolver import (
    EVOLVABLE_COMPONENTS,
    EvolutionResult,
    HarnessEvolver,
)
from symbiote.harness.versions import HarnessVersionRepository
from symbiote.knowledge.service import KnowledgeService
from symbiote.memory.store import MemoryStore
from symbiote.runners.loop_control import LoopController

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "evo_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name="EvoBot", role="assistant")
    return sym.id


@pytest.fixture()
def env_manager(adapter: SQLiteAdapter) -> EnvironmentManager:
    return EnvironmentManager(storage=adapter)


@pytest.fixture()
def versions(adapter: SQLiteAdapter) -> HarnessVersionRepository:
    return HarnessVersionRepository(storage=adapter)


@pytest.fixture()
def evolver(adapter: SQLiteAdapter, versions: HarnessVersionRepository) -> HarnessEvolver:
    return HarnessEvolver(storage=adapter, versions=versions)


def _insert_score(adapter, symbiote_id, final_score, stop_reason="end_turn", iters=2, tools=2):
    from datetime import UTC, datetime
    from uuid import uuid4

    session_id = str(uuid4())
    adapter.execute(
        "INSERT INTO session_scores "
        "(id, session_id, symbiote_id, auto_score, final_score, "
        "stop_reason, total_iterations, total_tool_calls, computed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid4()), session_id, symbiote_id, final_score, final_score,
         stop_reason, iters, tools, datetime.now(tz=UTC).isoformat()),
    )
    adapter.execute(
        "INSERT INTO execution_traces "
        "(id, session_id, symbiote_id, total_iterations, total_tool_calls, "
        "total_elapsed_ms, stop_reason, steps_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid4()), session_id, symbiote_id, iters, tools, 100,
         stop_reason, "[]", datetime.now(tz=UTC).isoformat()),
    )


# ── H-09.1: Bridge — overrides flow through the system ──────────────────────


class TestAssembledContextOverrides:
    def test_default_overrides_are_none(self) -> None:
        ctx = AssembledContext(
            symbiote_id="s", session_id="ss", user_input="hi",
        )
        assert ctx.tool_instructions_override is None
        assert ctx.injection_stagnation_override is None
        assert ctx.injection_circuit_breaker_override is None

    def test_overrides_set_explicitly(self) -> None:
        ctx = AssembledContext(
            symbiote_id="s", session_id="ss", user_input="hi",
            tool_instructions_override="Custom tools",
            injection_stagnation_override="Custom stag",
            injection_circuit_breaker_override="Custom cb",
        )
        assert ctx.tool_instructions_override == "Custom tools"
        assert ctx.injection_stagnation_override == "Custom stag"


class TestContextAssemblerResolvesVersions:
    def test_no_versions_returns_none_overrides(
        self, adapter, symbiote_id, env_manager,
    ) -> None:
        versions = HarnessVersionRepository(adapter)
        assembler = ContextAssembler(
            identity=IdentityManager(adapter),
            memory=MemoryStore(adapter),
            knowledge=KnowledgeService(adapter),
            environment=env_manager,
            harness_versions=versions,
        )
        from symbiote.core.session import SessionManager
        session = SessionManager(adapter).start(symbiote_id=symbiote_id)

        ctx = assembler.build(session.id, symbiote_id, "test")
        assert ctx.tool_instructions_override is None

    def test_with_version_returns_override(
        self, adapter, symbiote_id, env_manager,
    ) -> None:
        versions = HarnessVersionRepository(adapter)
        versions.create_version(symbiote_id, "tool_instructions", "Be very concise.")

        assembler = ContextAssembler(
            identity=IdentityManager(adapter),
            memory=MemoryStore(adapter),
            knowledge=KnowledgeService(adapter),
            environment=env_manager,
            harness_versions=versions,
        )
        from symbiote.core.session import SessionManager
        session = SessionManager(adapter).start(symbiote_id=symbiote_id)

        ctx = assembler.build(session.id, symbiote_id, "test")
        assert ctx.tool_instructions_override == "Be very concise."


class TestLoopControllerCustomMessages:
    def test_default_messages(self) -> None:
        ctrl = LoopController(max_iterations=10)
        ctrl.record("t", {"x": 1}, True)
        ctrl.record("t", {"x": 1}, True)
        msg = ctrl.get_injection_message()
        assert "repeating the same action" in msg

    def test_custom_stagnation_message(self) -> None:
        ctrl = LoopController(
            max_iterations=10,
            stagnation_msg="STOP NOW. Task is done.",
        )
        ctrl.record("t", {"x": 1}, True)
        ctrl.record("t", {"x": 1}, True)
        msg = ctrl.get_injection_message()
        assert msg == "STOP NOW. Task is done."

    def test_custom_circuit_breaker_message(self) -> None:
        ctrl = LoopController(
            max_iterations=10,
            circuit_breaker_msg="Tool '{tool_id}' broke {count}x. Give up.",
        )
        ctrl.record("api", {}, False)
        ctrl.record("api", {}, False)
        ctrl.record("api", {}, False)
        msg = ctrl.get_injection_message()
        assert msg == "Tool 'api' broke 3x. Give up."

    def test_circuit_breaker_default_format(self) -> None:
        ctrl = LoopController(max_iterations=10)
        ctrl.record("flaky", {}, False)
        ctrl.record("flaky", {}, False)
        ctrl.record("flaky", {}, False)
        msg = ctrl.get_injection_message()
        assert "flaky" in msg
        assert "3 times" in msg


class TestChatRunnerUsesOverride:
    def test_build_system_uses_override(self, symbiote_id) -> None:
        from symbiote.runners.chat import ChatRunner

        class MockLLM:
            def complete(self, messages, config=None, tools=None):
                return "ok"

        runner = ChatRunner(MockLLM())
        ctx = AssembledContext(
            symbiote_id=symbiote_id, session_id="s", user_input="hi",
            available_tools=[{
                "tool_id": "t", "name": "T", "description": "d",
                "parameters": {},
            }],
            tool_instructions_override="CUSTOM TOOL RULES HERE",
            tool_loop=True,
        )
        system = runner._build_system(ctx)
        assert "CUSTOM TOOL RULES HERE" in system
        assert "Do not narrate" not in system  # default not present

    def test_build_system_fallback_to_default(self, symbiote_id) -> None:
        from symbiote.runners.chat import ChatRunner

        class MockLLM:
            def complete(self, messages, config=None, tools=None):
                return "ok"

        runner = ChatRunner(MockLLM())
        ctx = AssembledContext(
            symbiote_id=symbiote_id, session_id="s", user_input="hi",
            available_tools=[{
                "tool_id": "t", "name": "T", "description": "d",
                "parameters": {},
            }],
            tool_loop=True,
        )
        system = runner._build_system(ctx)
        assert "Do not narrate" in system  # default present


# ── H-09.2: HarnessEvolver ──────────────────────────────────────────────────


class TestEvolverGuardRails:
    def test_too_long_rejected(self, evolver) -> None:
        result = evolver._check_guard_rails("short", "x" * 1000)
        assert result is not None
        assert "Too long" in result

    def test_too_short_rejected(self, evolver) -> None:
        result = evolver._check_guard_rails("normal text here", "hi")
        assert result is not None
        assert "Too short" in result

    def test_critical_line_missing_rejected(self, evolver) -> None:
        current = "Line 1\n- CRITICAL — do not skip this\nLine 3"
        proposal = "Line 1\nLine 3 modified"
        result = evolver._check_guard_rails(current, proposal)
        assert result is not None
        assert "CRITICAL" in result

    def test_critical_line_preserved_passes(self, evolver) -> None:
        current = "Line 1\n- CRITICAL — do not skip this\nLine 3"
        proposal = "Better line 1\n- CRITICAL — do not skip this\nBetter line 3"
        result = evolver._check_guard_rails(current, proposal)
        assert result is None

    def test_json_rejected(self, evolver) -> None:
        current = "You are an agent. " * 5  # long enough for 2x
        result = evolver._check_guard_rails(current, '{"tool": "x", "params": {}}')
        assert result is not None
        assert "JSON" in result

    def test_code_block_rejected(self, evolver) -> None:
        current = "You are an agent. " * 5
        result = evolver._check_guard_rails(current, "```python\nprint('hello world')\nmore code here\n```")
        assert result is not None
        assert "code block" in result

    def test_python_code_rejected(self, evolver) -> None:
        current = "You are an agent. " * 5
        result = evolver._check_guard_rails(current, "def evolve():\n    pass\n" + "x" * 30)
        assert result is not None
        assert "Python" in result

    def test_valid_proposal_passes(self, evolver) -> None:
        current = "You are an agent. Use tools wisely."
        proposal = "You are an autonomous agent. Use tools effectively and efficiently."
        result = evolver._check_guard_rails(current, proposal)
        assert result is None


class TestEvolverFlow:
    def test_no_llm_returns_failure(self, adapter, versions, symbiote_id) -> None:
        evolver = HarnessEvolver(storage=adapter, versions=versions, proposer_llm=None)
        result = evolver.evolve(symbiote_id, "tool_instructions", "default")
        assert result.success is False
        assert "No proposer LLM" in result.reason

    def test_invalid_component_returns_failure(self, evolver, symbiote_id) -> None:
        result = evolver.evolve(symbiote_id, "nonexistent", "default")
        assert result.success is False
        assert "not evolvable" in result.reason

    def test_not_enough_failed_sessions(self, evolver, adapter, symbiote_id) -> None:
        class MockLLM:
            def complete(self, messages, config=None, tools=None):
                return "improved"

        evolver.set_proposer_llm(MockLLM())
        # Only 2 failed sessions (need 5)
        for _ in range(2):
            _insert_score(adapter, symbiote_id, 0.1, "max_iterations")
        for _ in range(5):
            _insert_score(adapter, symbiote_id, 0.9)

        result = evolver.evolve(symbiote_id, "tool_instructions", "default")
        assert result.success is False
        assert "Not enough failed" in result.reason

    def test_successful_evolution(self, evolver, adapter, versions, symbiote_id) -> None:
        default = "You are an autonomous agent. Use tools to complete tasks. Be concise."

        class MockLLM:
            def complete(self, messages, config=None, tools=None):
                return "You are an autonomous agent. Use tools effectively and verify results."

        evolver.set_proposer_llm(MockLLM())
        for _ in range(8):
            _insert_score(adapter, symbiote_id, 0.2, "stagnation")
        for _ in range(5):
            _insert_score(adapter, symbiote_id, 0.9)

        result = evolver.evolve(symbiote_id, "tool_instructions", default)
        assert result.success is True
        assert result.new_version == 1

        # Verify persisted
        content = versions.get_active(symbiote_id, "tool_instructions")
        assert "verify results" in content

    def test_guard_rail_blocks_bad_proposal(self, evolver, adapter, symbiote_id) -> None:
        default = "You are an autonomous agent. Use tools to complete tasks. Be concise."

        class BadLLM:
            def complete(self, messages, config=None, tools=None):
                return '{"error": "I am JSON not instructions"}'

        evolver.set_proposer_llm(BadLLM())
        for _ in range(8):
            _insert_score(adapter, symbiote_id, 0.2)
        for _ in range(5):
            _insert_score(adapter, symbiote_id, 0.9)

        result = evolver.evolve(symbiote_id, "tool_instructions", default)
        assert result.success is False
        assert result.guard_rail_failed is not None
        assert "JSON" in result.guard_rail_failed

    def test_strip_markdown_wrapper(self, evolver) -> None:
        wrapped = "```\nClean text here\n```"
        assert evolver._strip_markdown(wrapped) == "Clean text here"

    def test_strip_markdown_noop(self, evolver) -> None:
        plain = "Plain text here"
        assert evolver._strip_markdown(plain) == "Plain text here"


class TestEvolverRollback:
    def test_no_active_version_no_rollback(self, evolver, symbiote_id) -> None:
        check = evolver.check_rollback(symbiote_id, "tool_instructions")
        assert check.should_rollback is False

    def test_not_enough_sessions_no_rollback(self, evolver, versions, symbiote_id) -> None:
        versions.create_version(symbiote_id, "tool_instructions", "V1")
        # Only 10 sessions tracked (need 50)
        for _ in range(10):
            versions.update_score(symbiote_id, "tool_instructions", 0.5)

        check = evolver.check_rollback(symbiote_id, "tool_instructions")
        assert check.should_rollback is False
        assert "Not enough sessions" in check.reason

    def test_rollback_when_score_drops(self, evolver, versions, adapter, symbiote_id) -> None:
        # V1 with good score
        versions.create_version(symbiote_id, "tool_instructions", "V1 good")
        for _ in range(60):
            versions.update_score(symbiote_id, "tool_instructions", 0.8)

        # V2 with bad score (parent=V1)
        versions.create_version(symbiote_id, "tool_instructions", "V2 bad", parent_version=1)
        for _ in range(55):
            versions.update_score(symbiote_id, "tool_instructions", 0.3)

        check = evolver.check_rollback(symbiote_id, "tool_instructions")
        assert check.should_rollback is True
        assert "Score dropped" in check.reason

    def test_no_rollback_when_score_acceptable(self, evolver, versions, symbiote_id) -> None:
        versions.create_version(symbiote_id, "tool_instructions", "V1")
        for _ in range(60):
            versions.update_score(symbiote_id, "tool_instructions", 0.7)

        versions.create_version(symbiote_id, "tool_instructions", "V2", parent_version=1)
        for _ in range(55):
            versions.update_score(symbiote_id, "tool_instructions", 0.72)

        check = evolver.check_rollback(symbiote_id, "tool_instructions")
        assert check.should_rollback is False

    def test_auto_rollback_performs_rollback(self, evolver, versions, symbiote_id) -> None:
        versions.create_version(symbiote_id, "tool_instructions", "V1 good")
        for _ in range(60):
            versions.update_score(symbiote_id, "tool_instructions", 0.8)

        versions.create_version(symbiote_id, "tool_instructions", "V2 bad", parent_version=1)
        for _ in range(55):
            versions.update_score(symbiote_id, "tool_instructions", 0.3)

        rolled_back = evolver.auto_rollback_if_needed(symbiote_id, "tool_instructions")
        assert rolled_back is True

        # V1 should be active again
        content = versions.get_active(symbiote_id, "tool_instructions")
        assert content == "V1 good"


class TestEvolvableComponents:
    def test_components_list(self) -> None:
        assert "tool_instructions" in EVOLVABLE_COMPONENTS
        assert "injection_stagnation" in EVOLVABLE_COMPONENTS
        assert "injection_circuit_breaker" in EVOLVABLE_COMPONENTS
        assert len(EVOLVABLE_COMPONENTS) == 3

    def test_non_evolvable_rejected(self, evolver, symbiote_id) -> None:
        class MockLLM:
            def complete(self, messages, config=None, tools=None):
                return "x"

        evolver.set_proposer_llm(MockLLM())
        result = evolver.evolve(symbiote_id, "_INDEX_INSTRUCTIONS", "default")
        assert result.success is False


# ── Integration: kernel.evolve_harness ───────────────────────────────────────


class TestKernelEvolution:
    def test_evolve_harness_with_evolver_llm(self, adapter) -> None:
        from symbiote.config.models import KernelConfig
        from symbiote.core.kernel import SymbioteKernel

        default = "You are an autonomous agent. Use tools to complete user tasks efficiently."

        class ProposerLLM:
            def complete(self, messages, config=None, tools=None):
                return "You are an autonomous agent. Use tools effectively and verify results."

        class MainLLM:
            def complete(self, messages, config=None, tools=None):
                return "I am the main LLM"

        config = KernelConfig(db_path=adapter._db_path)
        kernel = SymbioteKernel(config, llm=MainLLM())
        kernel.set_evolver_llm(ProposerLLM())

        sym = kernel.create_symbiote(name="EvoTest", role="test")

        for _ in range(8):
            _insert_score(adapter, sym.id, 0.2, "stagnation")
        for _ in range(5):
            _insert_score(adapter, sym.id, 0.9)

        result = kernel.evolve_harness(sym.id, "tool_instructions", default)
        assert result.success is True
        assert result.new_version == 1

        content = kernel.harness_versions.get_active(sym.id, "tool_instructions")
        assert "verify results" in content

        kernel.shutdown()

    def test_evolve_falls_back_to_main_llm(self, adapter) -> None:
        from symbiote.config.models import KernelConfig
        from symbiote.core.kernel import SymbioteKernel

        default = "You are an autonomous agent. Use tools to complete user tasks efficiently."

        class MainLLM:
            def complete(self, messages, config=None, tools=None):
                return "You are an autonomous agent. Handle tools with care and precision."

        config = KernelConfig(db_path=adapter._db_path)
        kernel = SymbioteKernel(config, llm=MainLLM())

        sym = kernel.create_symbiote(name="FallbackTest", role="test")
        for _ in range(8):
            _insert_score(adapter, sym.id, 0.1)
        for _ in range(5):
            _insert_score(adapter, sym.id, 0.9)

        result = kernel.evolve_harness(sym.id, "tool_instructions", default)
        assert result.success is True

        kernel.shutdown()
