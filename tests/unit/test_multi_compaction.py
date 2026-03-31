"""Tests for 3-layer compaction system in ChatRunner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.context import AssembledContext
from symbiote.core.identity import IdentityManager
from symbiote.environment.descriptors import ToolDescriptor
from symbiote.environment.manager import EnvironmentManager
from symbiote.environment.policies import PolicyGate
from symbiote.environment.tools import ToolGateway
from symbiote.runners.chat import (
    _MICROCOMPACT_MAX_CHARS,
    ChatRunner,
)


class MockLLM:
    def __init__(self, response: str) -> None:
        self._response = response

    def complete(self, messages, config=None, tools=None):
        return self._response


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "compaction_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name="CompactBot", role="assistant")
    return sym.id


def _make_context(
    symbiote_id: str, user_input: str = "test",
    tools: list[dict] | None = None,
    tool_loop: bool = False,
) -> AssembledContext:
    return AssembledContext(
        symbiote_id=symbiote_id,
        session_id="sess-compact",
        user_input=user_input,
        available_tools=tools or [],
        tool_loop=tool_loop,
    )


# ── Layer 1: Microcompact ─────────────────────────────────────────────────


class TestMicrocompact:
    def test_short_result_unchanged(self) -> None:
        text = "[Tool result: search]\n{\"items\": [1, 2, 3]}"
        assert ChatRunner._microcompact_tool_result(text) == text

    def test_long_result_truncated(self) -> None:
        # Build a result longer than _MICROCOMPACT_MAX_CHARS
        big_json = json.dumps({"data": "x" * 5000})
        text = f"[Tool result: fetch]\n{big_json}"
        result = ChatRunner._microcompact_tool_result(text)

        assert len(result) < len(text)
        assert "truncated" in result
        assert result.startswith("[Tool result: fetch]")

    def test_exact_threshold_not_truncated(self) -> None:
        text = "x" * _MICROCOMPACT_MAX_CHARS
        assert ChatRunner._microcompact_tool_result(text) == text

    def test_one_over_threshold_truncated(self) -> None:
        text = "x" * (_MICROCOMPACT_MAX_CHARS + 1)
        result = ChatRunner._microcompact_tool_result(text)
        assert "truncated" in result

    def test_format_tool_results_applies_microcompact(self, symbiote_id: str) -> None:
        """_format_tool_results should microcompact large results."""
        from symbiote.environment.descriptors import ToolCallResult

        big_output = {"data": "y" * 5000}
        results = [
            ToolCallResult(tool_id="big_tool", success=True, output=big_output),
        ]
        formatted = ChatRunner._format_tool_results(results)
        assert "truncated" in formatted


# ── Layer 2: Loop Compaction (existing, verify integration) ───────────────


class TestLoopCompaction:
    def test_no_compact_under_threshold(self) -> None:
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ]
        initial = len(messages)
        # Add 2 pairs (4 messages) — threshold is 4 pairs (8 messages)
        for i in range(2):
            messages.append({"role": "assistant", "content": f"call {i}"})
            messages.append({"role": "user", "content": f"[Tool result: t{i}]\nok"})

        ChatRunner._compact_loop_messages(messages, initial)
        # Should not compact — only 4 loop messages, threshold is 8
        assert len(messages) == initial + 4

    def test_compact_over_threshold(self) -> None:
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ]
        initial = len(messages)
        # Add 5 pairs (10 messages) — above threshold of 4 pairs
        for i in range(5):
            messages.append({"role": "assistant", "content": f"call {i}"})
            messages.append({"role": "user", "content": f"[Tool result: t{i}]\nresult {i}"})

        ChatRunner._compact_loop_messages(messages, initial)
        # Should compact: initial + summary + last pair (2) = initial + 3
        assert len(messages) == initial + 3
        assert "compacted" in messages[initial]["content"].lower()


# ── Layer 3: Autocompact ─────────────────────────────────────────────────


class TestAutocompact:
    def test_no_autocompact_under_budget(self) -> None:
        runner = ChatRunner(MockLLM("ok"), context_budget=16000)
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ]
        initial = len(messages)
        # Small loop messages — well under budget
        for i in range(3):
            messages.append({"role": "assistant", "content": f"call {i}"})
            messages.append({"role": "user", "content": f"[Tool result: t{i}]\nok"})

        result = runner._autocompact_if_needed(messages, initial)
        assert result is False
        assert len(messages) == initial + 6

    def test_autocompact_triggers_over_budget(self) -> None:
        # Set a very small budget to force autocompact
        runner = ChatRunner(MockLLM("ok"), context_budget=100)
        messages = [
            {"role": "system", "content": "system prompt " * 10},
            {"role": "user", "content": "hello"},
        ]
        initial = len(messages)
        # Add loop messages that will push us over the tiny budget
        for i in range(3):
            messages.append({"role": "assistant", "content": f"call {i} " * 20})
            messages.append({"role": "user", "content": f"[Tool result: t{i}]\n{'data ' * 20}"})

        result = runner._autocompact_if_needed(messages, initial)
        assert result is True
        # After autocompact: initial messages + 1 summary
        assert len(messages) == initial + 1
        assert "autocompact" in messages[initial]["content"].lower()

    def test_autocompact_summary_contains_tool_ids(self) -> None:
        runner = ChatRunner(MockLLM("ok"), context_budget=50)
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ]
        initial = len(messages)
        messages.append({"role": "assistant", "content": "calling search"})
        messages.append({"role": "user", "content": "[Tool result: items_search]\n" + "x" * 500})
        messages.append({"role": "assistant", "content": "calling publish"})
        messages.append({"role": "user", "content": "[Tool error: items_publish]\nfailed"})

        runner._autocompact_if_needed(messages, initial)
        summary = messages[initial]["content"]
        assert "items_search" in summary
        assert "items_publish" in summary
        assert "error" in summary

    def test_autocompact_no_loop_messages(self) -> None:
        runner = ChatRunner(MockLLM("ok"), context_budget=10)
        messages = [
            {"role": "system", "content": "sys " * 100},
            {"role": "user", "content": "hello"},
        ]
        initial = len(messages)
        # No loop messages to compact
        result = runner._autocompact_if_needed(messages, initial)
        assert result is False

    def test_autocompact_single_pair(self) -> None:
        """Single pair (2 messages) should NOT be compacted — not enough data."""
        runner = ChatRunner(MockLLM("ok"), context_budget=10)
        messages = [
            {"role": "system", "content": "sys " * 100},
            {"role": "user", "content": "hello"},
        ]
        initial = len(messages)
        messages.append({"role": "assistant", "content": "call"})
        # Only 1 message in loop — less than 2 required
        result = runner._autocompact_if_needed(messages, initial)
        assert result is False


# ── Integration: All 3 layers work together ──────────────────────────────


class TestMultiLayerIntegration:
    def test_layers_dont_interfere(self, symbiote_id: str, adapter, env_manager: EnvironmentManager) -> None:
        """Run a full loop that exercises microcompact + loop compact.

        Uses a MockLLM that calls tools producing large results, then
        stops. Verifies that the runner completes successfully and results
        are present.
        """
        call_count = 0

        class SequenceLLM:
            def complete(self, messages, config=None, tools=None):
                nonlocal call_count
                call_count += 1
                if call_count <= 3:
                    return (
                        f'```tool_call\n'
                        f'{{"tool": "big_tool", "params": {{"i": {call_count}}}}}\n'
                        f'```'
                    )
                return "All done!"

        gate = PolicyGate(env_manager=env_manager, storage=adapter)
        gw = ToolGateway(policy_gate=gate)

        desc = ToolDescriptor(
            tool_id="big_tool",
            name="Big Tool",
            description="Returns large data",
            parameters={"type": "object", "properties": {"i": {"type": "integer"}}},
        )
        gw.register_descriptor(desc, lambda params: {"data": "x" * 5000})

        env_manager.configure(symbiote_id, tools=["big_tool"])

        runner = ChatRunner(SequenceLLM(), tool_gateway=gw, context_budget=8000)
        context = _make_context(symbiote_id, "do stuff", tool_loop=True,
                                tools=[desc.model_dump()])

        result = runner.run(context)
        assert result.success is True
        assert isinstance(result.output, dict)
        assert result.output["text"] == "All done!"
        assert len(result.output["tool_results"]) == 3


@pytest.fixture()
def env_manager(adapter: SQLiteAdapter) -> EnvironmentManager:
    return EnvironmentManager(storage=adapter)
