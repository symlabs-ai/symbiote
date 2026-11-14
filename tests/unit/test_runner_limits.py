"""Host-configurable limits of the runners: the runtime block prepended to
the user message, and the per-result ceiling inside long-run blocks
(SymGateway feedback on 0.8.2)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError as ConfigError

from symbiote.core.context import AssembledContext
from symbiote.core.exceptions import ValidationError
from symbiote.runners.chat import ChatRunner
from tests.support import ScriptedLLM, add_tool, make_kernel, tool_call_block

PLAN = json.dumps([{"name": "A", "description": "d", "success_criteria": "c"}])
VERDICT = json.dumps(
    {"passed": True, "overall_score": 0.9, "feedback": "f", "blocking_issues": []}
)


def _user_message(mode: str | None) -> str:
    llm = ScriptedLLM("ok")
    kwargs = {} if mode is None else {"runtime_context": mode}
    ChatRunner(llm, **kwargs).run(
        AssembledContext(symbiote_id="sym", session_id="sess-99", user_input="Hello")
    )
    return llm.calls[0]["messages"][-1]["content"]


class TestRuntimeContextModes:
    def test_default_keeps_timestamp_and_session(self) -> None:
        content = _user_message(None)

        assert "Timestamp: " in content
        assert "Session: sess-99" in content
        assert content.endswith("Hello")

    def test_timestamp_mode_leaves_the_session_id_out(self) -> None:
        content = _user_message("timestamp")

        assert "Timestamp: " in content
        assert "sess-99" not in content
        assert content.endswith("Hello")

    def test_off_sends_the_message_as_written(self) -> None:
        assert _user_message("off") == "Hello"

    def test_an_unknown_mode_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            ChatRunner(ScriptedLLM("ok"), runtime_context="none")
        with pytest.raises(ConfigError):
            make_kernel(Path("unused"), runtime_context="none")

    @pytest.mark.parametrize(
        ("mode", "has_time", "has_session"),
        [("full", True, True), ("timestamp", True, False), ("off", False, False)],
    )
    def test_the_kernel_setting_reaches_the_model_call(
        self, tmp_path: Path, mode: str, has_time: bool, has_session: bool
    ) -> None:
        llm = ScriptedLLM("ok")
        kernel = make_kernel(tmp_path, llm, runtime_context=mode)
        try:
            symbiote = kernel.create_symbiote(name="a", role="r")
            session = kernel.start_session(symbiote.id)

            kernel.message(session.id, "Hello")

            content = llm.calls[0]["messages"][-1]["content"]
            assert ("Timestamp: " in content) is has_time
            assert (session.id in content) is has_session
        finally:
            kernel.shutdown()


def _long_run_block_prompts(tmp_path: Path, **config) -> list[str]:
    """Run one long-run block that calls ``big`` once; return what the block's
    model calls received."""
    block_calls: list[str] = []

    def fn(_i: int, messages: list[dict]) -> str:
        system = str(messages[0].get("content"))
        if system.startswith("PLANNER-PROMPT"):
            return PLAN
        if system.startswith("EVAL"):
            return VERDICT
        text = "\n".join(str(m.get("content")) for m in messages)
        block_calls.append(text)
        return "block done" if "[Tool result: big]" in text else tool_call_block("big")

    kernel = make_kernel(tmp_path, ScriptedLLM(fn), **config)
    try:
        symbiote = kernel.create_symbiote(name="a", role="assistant")
        add_tool(
            kernel,
            symbiote.id,
            "big",
            lambda _params: "x" * 5000 + "END-OF-PAYLOAD",
            tool_mode="long_run",
            planner_prompt="PLANNER-PROMPT: decompose",
            evaluator_prompt="EVAL",
        )
        session = kernel.start_session(symbiote.id)
        kernel.message(session.id, "build it")
    finally:
        kernel.shutdown()
    return block_calls


class TestLongRunBlocksFollowTheKernelConfig:
    def test_the_result_ceiling_applies_inside_a_block(self, tmp_path: Path) -> None:
        prompts = _long_run_block_prompts(
            tmp_path, tool_result_max_chars=30_000, loop_context_budget=64_000
        )

        assert "END-OF-PAYLOAD" in prompts[-1]
        assert "chars truncated" not in prompts[-1]

    def test_the_default_ceiling_still_cuts(self, tmp_path: Path) -> None:
        prompts = _long_run_block_prompts(tmp_path)

        assert "[Tool result: big]" in prompts[-1]
        assert "END-OF-PAYLOAD" not in prompts[-1]
        assert "chars truncated" in prompts[-1]

    def test_the_runtime_context_setting_applies_inside_a_block(
        self, tmp_path: Path
    ) -> None:
        prompts = _long_run_block_prompts(tmp_path, runtime_context="off")

        assert all("[Runtime Context" not in prompt for prompt in prompts)


class TestACutResultIsReported:
    """The host receives the whole result, so the log is what tells it that
    the model read less."""

    def test_the_log_names_the_tool_the_sizes_and_the_setting(self, caplog) -> None:
        from symbiote.agent_protocols.base import compact_tool_result

        with caplog.at_level(logging.DEBUG, logger="symbiote.agent_protocols.base"):
            first = compact_tool_result("s3cr3t" * 500, 2000, tool_id="report_cut_a")
            compact_tool_result("s3cr3t" * 500, 2000, tool_id="report_cut_a")
            compact_tool_result("short", 2000, tool_id="report_cut_a")

        assert first.endswith("... (1000 chars truncated)")
        records = [r for r in caplog.records if "report_cut_a" in r.getMessage()]
        assert [r.levelno for r in records] == [logging.WARNING, logging.DEBUG]
        message = records[0].getMessage()
        assert "3000 chars" in message
        assert "limit 2000" in message
        assert "tool_result_max_chars" in message
        assert "loop_context_budget" in message
        assert "s3cr3t" not in caplog.text  # sizes only, never the content

    def test_a_turn_that_cuts_a_result_warns_the_host(
        self, tmp_path: Path, caplog
    ) -> None:
        def fn(_i: int, messages: list[dict]) -> str:
            text = "\n".join(str(m.get("content")) for m in messages)
            return "done" if "[Tool result: report_cut_b]" in text else tool_call_block("report_cut_b")

        kernel = make_kernel(tmp_path, ScriptedLLM(fn))
        try:
            symbiote = kernel.create_symbiote(name="a", role="r")
            add_tool(kernel, symbiote.id, "report_cut_b", lambda _p: "x" * 5000)
            session = kernel.start_session(symbiote.id)
            with caplog.at_level(logging.WARNING):
                kernel.message(session.id, "go")
        finally:
            kernel.shutdown()

        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("tool=report_cut_b" in w and "tool_result_max_chars" in w for w in warnings)
