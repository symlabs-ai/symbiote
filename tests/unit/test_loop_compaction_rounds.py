"""The round-count compaction of the tool loop (Layer 2) is a KernelConfig
setting, ``loop_compaction_rounds`` (B-110, SymCoder request).

Layer 2 replaces every older exchange of the loop with one line per step
after N rounds, whatever the loop budget. A model that reads files over many
rounds forgets what it read; with the setting off, only the budget
compaction (Layer 3, 80% of ``loop_context_budget``) shortens the loop.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError as ConfigError

from symbiote.config.models import KernelConfig
from symbiote.core.exceptions import ValidationError
from symbiote.runners.chat import ChatRunner
from tests.support import ScriptedLLM, add_tool, make_kernel, tool_call_block

ROUNDS = 6
PLAN = json.dumps([{"name": "A", "description": "d", "success_criteria": "c"}])
VERDICT = json.dumps(
    {"passed": True, "overall_score": 0.9, "feedback": "f", "blocking_issues": []}
)


def _read_files(i: int, _messages: list[dict]) -> str:
    return tool_call_block("read_file", n=i) if i < ROUNDS else "done"


def _file(params: dict) -> str:
    return f"CONTENT-{params['n']}"


def _last_prompt(tmp_path: Path, **config) -> str:
    """Run a chat turn that reads ROUNDS files; return the last model call."""
    llm = ScriptedLLM(_read_files)
    kernel = make_kernel(tmp_path, llm, **config)
    try:
        symbiote = kernel.create_symbiote(name="a", role="assistant")
        add_tool(kernel, symbiote.id, "read_file", _file)
        session = kernel.start_session(symbiote.id)
        kernel.message(session.id, "change the api")
    finally:
        kernel.shutdown()
    assert len(llm.calls) == ROUNDS + 1
    return llm.prompt_text()


class TestTheDefaultIsUnchanged:
    def test_four_rounds_by_default(self, tmp_path: Path) -> None:
        assert KernelConfig().loop_compaction_rounds == 4
        kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
        try:
            assert kernel._runner_registry.get("chat")._loop_compaction_rounds == 4
            assert kernel._runner_registry.get("long_run")._loop_compaction_rounds == 4
        finally:
            kernel.shutdown()

    def test_older_rounds_are_summarized_even_with_room_in_the_budget(
        self, tmp_path: Path
    ) -> None:
        text = _last_prompt(tmp_path, loop_context_budget=120_000)

        assert f"CONTENT-{ROUNDS - 1}" in text  # the latest exchange, in full
        assert '[Tool result: read_file]\n"CONTENT-0"' not in text
        assert "Context compacted" in text

    def test_a_custom_round_count_waits_longer(self, tmp_path: Path) -> None:
        text = _last_prompt(
            tmp_path, loop_context_budget=120_000, loop_compaction_rounds=ROUNDS + 1
        )

        assert all(f'[Tool result: read_file]\n"CONTENT-{n}"' in text for n in range(ROUNDS))
        assert "compacted" not in text.lower()


class TestTheLayerTurnedOff:
    @pytest.mark.parametrize("off", [None, 0])
    def test_the_history_stays_in_full_within_the_budget(
        self, tmp_path: Path, off: int | None
    ) -> None:
        text = _last_prompt(
            tmp_path, loop_context_budget=120_000, loop_compaction_rounds=off
        )

        assert all(f'[Tool result: read_file]\n"CONTENT-{n}"' in text for n in range(ROUNDS))
        assert "compacted" not in text.lower()

    def test_the_budget_still_compacts(self, tmp_path: Path) -> None:
        llm = ScriptedLLM(_read_files)
        kernel = make_kernel(
            tmp_path, llm, loop_context_budget=2_000, loop_compaction_rounds=None
        )
        try:
            symbiote = kernel.create_symbiote(name="a", role="assistant")
            add_tool(
                kernel, symbiote.id, "read_file", lambda p: _file(p) + "p" * 3000
            )
            session = kernel.start_session(symbiote.id)
            kernel.message(session.id, "change the api")
        finally:
            kernel.shutdown()

        text = llm.prompt_text()
        assert f"CONTENT-{ROUNDS - 1}" in text  # the latest result is visible
        assert "CONTENT-0" not in text  # older ones were summarized by Layer 3
        assert "[Autocompact" in text
        assert "Context compacted" not in text

    def test_the_runner_alone(self) -> None:
        messages = [{"role": "user", "content": "go"}]
        for i in range(ROUNDS):
            messages.append({"role": "assistant", "content": f"call {i}"})
            messages.append({"role": "user", "content": f"[Tool result: t]\nR{i}"})
        before = list(messages)

        ChatRunner._compact_loop_messages(messages, 1, rounds=None)
        ChatRunner._compact_loop_messages(messages, 1, rounds=0)

        assert messages == before

    def test_a_negative_count_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError):
            ChatRunner(ScriptedLLM("ok"), loop_compaction_rounds=-1)
        with pytest.raises(ConfigError):
            KernelConfig(loop_compaction_rounds=-1)


def _long_run_block_prompts(tmp_path: Path, **config) -> list[str]:
    """Run one long-run block that reads ROUNDS files; return what the
    block's model calls received."""
    block_calls: list[str] = []

    def fn(_i: int, messages: list[dict]) -> str:
        system = str(messages[0].get("content"))
        if system.startswith("PLANNER-PROMPT"):
            return PLAN
        if system.startswith("EVAL"):
            return VERDICT
        if system.startswith("You ran a multi-step project"):
            return "answer"
        block_calls.append("\n".join(str(m.get("content")) for m in messages))
        n = len(block_calls) - 1
        return tool_call_block("read_file", n=n) if n < ROUNDS else "block done"

    kernel = make_kernel(tmp_path, ScriptedLLM(fn), loop_context_budget=120_000, **config)
    try:
        symbiote = kernel.create_symbiote(name="a", role="assistant")
        add_tool(
            kernel,
            symbiote.id,
            "read_file",
            _file,
            tool_mode="long_run",
            planner_prompt="PLANNER-PROMPT: decompose",
            evaluator_prompt="EVAL",
        )
        session = kernel.start_session(symbiote.id)
        kernel.message(session.id, "build it")
    finally:
        kernel.shutdown()
    assert len(block_calls) == ROUNDS + 1
    return block_calls


class TestLongRunBlocks:
    def test_blocks_summarize_by_default(self, tmp_path: Path) -> None:
        last = _long_run_block_prompts(tmp_path)[-1]

        assert f"CONTENT-{ROUNDS - 1}" in last
        assert '[Tool result: read_file]\n"CONTENT-0"' not in last
        assert "Context compacted" in last

    def test_blocks_follow_the_setting(self, tmp_path: Path) -> None:
        last = _long_run_block_prompts(tmp_path, loop_compaction_rounds=None)[-1]

        assert all(f'[Tool result: read_file]\n"CONTENT-{n}"' in last for n in range(ROUNDS))
        assert "compacted" not in last.lower()
