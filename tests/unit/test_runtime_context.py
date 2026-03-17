"""Tests for runtime context injection and stripping — B-9."""

from __future__ import annotations

from datetime import UTC, datetime

from symbiote.environment.runtime_context import (
    build_runtime_block,
    inject_runtime_context,
    strip_runtime_context,
)


class TestBuildRuntimeBlock:
    def test_includes_timestamp(self) -> None:
        ts = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        block = build_runtime_block(timestamp=ts)
        assert "2026-03-16 12:00:00 UTC" in block

    def test_includes_session_id(self) -> None:
        block = build_runtime_block(session_id="sess-42")
        assert "Session: sess-42" in block

    def test_includes_extra_fields(self) -> None:
        block = build_runtime_block(extra={"Workspace": "/tmp/ws", "Channel": "telegram"})
        assert "Workspace: /tmp/ws" in block
        assert "Channel: telegram" in block

    def test_has_delimiters(self) -> None:
        block = build_runtime_block()
        assert block.startswith("[Runtime Context")
        assert block.endswith("[/Runtime Context]")


class TestInjectRuntimeContext:
    def test_prepends_block_to_message(self) -> None:
        result = inject_runtime_context("Hello world", session_id="s1")
        assert result.endswith("Hello world")
        assert "[Runtime Context" in result
        assert "Session: s1" in result

    def test_original_message_preserved(self) -> None:
        msg = "What is the weather today?"
        result = inject_runtime_context(msg)
        assert msg in result


class TestStripRuntimeContext:
    def test_strips_block_cleanly(self) -> None:
        injected = inject_runtime_context("Hello", session_id="s1")
        stripped = strip_runtime_context(injected)
        assert stripped == "Hello"

    def test_no_block_returns_unchanged(self) -> None:
        msg = "Just a normal message"
        assert strip_runtime_context(msg) == msg

    def test_strips_with_extra_fields(self) -> None:
        injected = inject_runtime_context(
            "Query here",
            session_id="s1",
            extra={"Workspace": "/tmp", "Channel": "web"},
        )
        stripped = strip_runtime_context(injected)
        assert stripped == "Query here"

    def test_roundtrip_preserves_content(self) -> None:
        original = "Multi\nline\nmessage"
        injected = inject_runtime_context(original, session_id="abc")
        stripped = strip_runtime_context(injected)
        assert stripped == original


class TestChatRunnerRuntimeIntegration:
    """Verify ChatRunner injects runtime context in LLM messages."""

    def test_user_message_has_runtime_context(self) -> None:
        from symbiote.core.context import AssembledContext
        from symbiote.runners.chat import ChatRunner

        messages_seen: list[list[dict]] = []

        class CaptureLLM:
            def complete(self, messages: list[dict], config: dict | None = None) -> str:
                messages_seen.append(messages)
                return "ok"

        runner = ChatRunner(CaptureLLM())
        ctx = AssembledContext(
            symbiote_id="s1",
            session_id="sess-99",
            user_input="Hello",
        )
        runner.run(ctx)

        assert len(messages_seen) == 1
        user_msg = messages_seen[0][-1]  # last message is user
        assert user_msg["role"] == "user"
        assert "[Runtime Context" in user_msg["content"]
        assert "Session: sess-99" in user_msg["content"]
        assert "Hello" in user_msg["content"]

    def test_working_memory_gets_clean_message(self) -> None:
        from symbiote.core.context import AssembledContext
        from symbiote.memory.working import WorkingMemory
        from symbiote.runners.chat import ChatRunner

        class SimpleLLM:
            def complete(self, messages: list[dict], config: dict | None = None) -> str:
                return "Response here"

        wm = WorkingMemory(session_id="sess-1")
        runner = ChatRunner(SimpleLLM(), working_memory=wm)
        ctx = AssembledContext(
            symbiote_id="s1",
            session_id="sess-1",
            user_input="Hello",
        )
        runner.run(ctx)

        # Working memory should have clean assistant message (no runtime context)
        assert len(wm.recent_messages) == 1
        assert wm.recent_messages[0].role == "assistant"
        assert "[Runtime Context" not in wm.recent_messages[0].content
