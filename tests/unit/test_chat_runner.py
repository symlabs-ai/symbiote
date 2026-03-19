"""Tests for ChatRunner — TDD RED phase."""

from __future__ import annotations

import pytest

from symbiote.core.context import AssembledContext
from symbiote.core.models import Message
from symbiote.core.ports import LLMPort
from symbiote.memory.working import WorkingMemory
from symbiote.runners.chat import ChatRunner

# ── Fake LLM ────────────────────────────────────────────────────────────────


class FakeLLM:
    """Implements LLMPort, returns a canned response."""

    def __init__(self, response: str = "Hello from LLM") -> None:
        self.response = response
        self.last_messages: list[dict] | None = None
        self.last_config: dict | None = None

    def complete(self, messages: list[dict], config: dict | None = None, tools: list[dict] | None = None) -> str:
        self.last_messages = messages
        self.last_config = config
        return self.response


class ErrorLLM:
    """LLM that always raises."""

    def complete(self, messages: list[dict], config: dict | None = None, tools: list[dict] | None = None) -> str:
        raise RuntimeError("LLM connection failed")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_context(
    user_input: str = "Hi there",
    persona: dict | None = None,
    relevant_memories: list[dict] | None = None,
    relevant_knowledge: list[dict] | None = None,
    working_memory_snapshot: dict | None = None,
) -> AssembledContext:
    return AssembledContext(
        symbiote_id="sym-1",
        session_id="sess-1",
        persona=persona,
        working_memory_snapshot=working_memory_snapshot,
        relevant_memories=relevant_memories or [],
        relevant_knowledge=relevant_knowledge or [],
        user_input=user_input,
    )


# ── can_handle tests ────────────────────────────────────────────────────────


class TestCanHandle:
    def test_handles_chat(self) -> None:
        runner = ChatRunner(llm=FakeLLM())
        assert runner.can_handle("chat") is True

    def test_handles_ask(self) -> None:
        runner = ChatRunner(llm=FakeLLM())
        assert runner.can_handle("ask") is True

    def test_handles_question(self) -> None:
        runner = ChatRunner(llm=FakeLLM())
        assert runner.can_handle("question") is True

    def test_handles_talk(self) -> None:
        runner = ChatRunner(llm=FakeLLM())
        assert runner.can_handle("talk") is True

    def test_rejects_unknown(self) -> None:
        runner = ChatRunner(llm=FakeLLM())
        assert runner.can_handle("unknown") is False

    def test_rejects_empty(self) -> None:
        runner = ChatRunner(llm=FakeLLM())
        assert runner.can_handle("") is False


# ── run tests ────────────────────────────────────────────────────────────────


class TestRun:
    def test_returns_llm_response(self) -> None:
        llm = FakeLLM(response="Sure, I can help!")
        runner = ChatRunner(llm=llm)
        ctx = _make_context(user_input="Help me")
        result = runner.run(ctx)

        assert result.success is True
        assert result.output == "Sure, I can help!"
        assert result.runner_type == "chat"
        assert result.error is None

    def test_updates_working_memory(self) -> None:
        llm = FakeLLM(response="Done!")
        wm = WorkingMemory(session_id="sess-1")
        runner = ChatRunner(llm=llm, working_memory=wm)
        ctx = _make_context()
        runner.run(ctx)

        assert len(wm.recent_messages) == 1
        msg = wm.recent_messages[0]
        assert msg.role == "assistant"
        assert msg.content == "Done!"
        assert msg.session_id == "sess-1"

    def test_llm_error_returns_failure(self) -> None:
        runner = ChatRunner(llm=ErrorLLM())
        ctx = _make_context()
        result = runner.run(ctx)

        assert result.success is False
        assert result.error == "LLM connection failed"
        assert result.runner_type == "chat"

    def test_system_message_includes_persona(self) -> None:
        llm = FakeLLM()
        persona = {"name": "Atlas", "summary": "A helpful coding assistant"}
        runner = ChatRunner(llm=llm)
        ctx = _make_context(persona=persona)
        runner.run(ctx)

        messages = llm.last_messages
        assert messages is not None
        system_msg = messages[0]
        assert system_msg["role"] == "system"
        assert "Atlas" in system_msg["content"]
        assert "helpful coding assistant" in system_msg["content"]

    def test_user_message_is_user_input(self) -> None:
        llm = FakeLLM()
        runner = ChatRunner(llm=llm)
        ctx = _make_context(user_input="What is Python?")
        runner.run(ctx)

        messages = llm.last_messages
        assert messages is not None
        # Last message should be user input
        user_msg = messages[-1]
        assert user_msg["role"] == "user"
        assert "What is Python?" in user_msg["content"]
        assert "[Runtime Context" in user_msg["content"]

    def test_includes_conversation_history(self) -> None:
        llm = FakeLLM()
        wm = WorkingMemory(session_id="sess-1")
        wm.update_message(Message(session_id="sess-1", role="user", content="Hi"))
        wm.update_message(
            Message(session_id="sess-1", role="assistant", content="Hello!")
        )
        runner = ChatRunner(llm=llm, working_memory=wm)
        ctx = _make_context(user_input="Follow up question")
        runner.run(ctx)

        messages = llm.last_messages
        assert messages is not None
        # system, history user, history assistant, current user
        assert len(messages) >= 4
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "Hi"
        assert messages[2]["role"] == "assistant"
        assert messages[2]["content"] == "Hello!"
        assert messages[-1]["role"] == "user"
        assert "Follow up question" in messages[-1]["content"]

    def test_system_message_includes_memories(self) -> None:
        llm = FakeLLM()
        runner = ChatRunner(llm=llm)
        ctx = _make_context(
            relevant_memories=[
                {"content": "User prefers dark mode", "type": "preference", "importance": 0.8}
            ]
        )
        runner.run(ctx)

        system_msg = llm.last_messages[0]
        assert "dark mode" in system_msg["content"]

    def test_system_message_includes_knowledge(self) -> None:
        llm = FakeLLM()
        runner = ChatRunner(llm=llm)
        ctx = _make_context(
            relevant_knowledge=[
                {"name": "Python docs", "content": "Python is a programming language"}
            ]
        )
        runner.run(ctx)

        system_msg = llm.last_messages[0]
        assert "Python is a programming language" in system_msg["content"]

    def test_runner_type_is_chat(self) -> None:
        runner = ChatRunner(llm=FakeLLM())
        assert runner.runner_type == "chat"


# ── on_token streaming hook ──────────────────────────────────────────────


class StreamingFakeLLM:
    """LLM that exposes a stream() method returning tokens one-by-one."""

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens

    def complete(self, messages, config=None, tools=None) -> str:
        return "".join(self._tokens)

    def stream(self, messages, config=None, tools=None):
        yield from self._tokens


class TestOnTokenHook:
    def test_on_token_called_with_streaming_llm(self) -> None:
        llm = StreamingFakeLLM(["Hello", " ", "world"])
        runner = ChatRunner(llm=llm)
        ctx = _make_context()

        received: list[str] = []
        result = runner.run(ctx, on_token=received.append)

        assert received == ["Hello", " ", "world"]
        assert result.output == "Hello world"

    def test_on_token_called_once_with_non_streaming_llm(self) -> None:
        llm = FakeLLM(response="Full response")
        runner = ChatRunner(llm=llm)
        ctx = _make_context()

        received: list[str] = []
        result = runner.run(ctx, on_token=received.append)

        assert received == ["Full response"]
        assert result.output == "Full response"

    def test_no_on_token_works_normally(self) -> None:
        llm = FakeLLM(response="Normal")
        runner = ChatRunner(llm=llm)
        ctx = _make_context()

        result = runner.run(ctx)

        assert result.output == "Normal"


# ── run_async ────────────────────────────────────────────────────────────


class TestRunAsync:
    @pytest.mark.asyncio
    async def test_run_async_returns_result(self) -> None:
        llm = FakeLLM(response="Async response")
        runner = ChatRunner(llm=llm)
        ctx = _make_context(user_input="Hello async")

        result = await runner.run_async(ctx)

        assert result.success is True
        assert result.output == "Async response"
        assert result.runner_type == "chat"

    @pytest.mark.asyncio
    async def test_run_async_on_token_with_streaming_llm(self) -> None:
        llm = StreamingFakeLLM(["token1", " ", "token2"])
        runner = ChatRunner(llm=llm)
        ctx = _make_context()

        received: list[str] = []
        result = await runner.run_async(ctx, on_token=received.append)

        assert received == ["token1", " ", "token2"]
        assert result.output == "token1 token2"

    @pytest.mark.asyncio
    async def test_run_async_llm_error_returns_failure(self) -> None:
        runner = ChatRunner(llm=ErrorLLM())
        ctx = _make_context()

        result = await runner.run_async(ctx)

        assert result.success is False
        assert result.error == "LLM connection failed"


# ── conversation_history promotion ──────────────────────────────────────────


class TestConversationHistoryPromotion:
    """conversation_history in extra_context should become real messages."""

    def test_history_becomes_real_messages(self) -> None:
        llm = FakeLLM(response="Journal Sandbox, certo!")
        runner = ChatRunner(llm=llm)
        ctx = AssembledContext(
            symbiote_id="sym-1",
            session_id="sess-1",
            user_input="1",
            extra_context={
                "conversation_history": (
                    "Usuário: Capture https://youtube.com/abc\n\n"
                    "Clark: Aqui estão seus journals:\n1. Sandbox\n2. Tech\nQual deseja?"
                ),
                "page_url": "/inbox",
            },
        )

        runner.run(ctx)

        msgs = llm.last_messages
        # system, user(history), assistant(history), user("1")
        assert len(msgs) == 4
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert "youtube.com" in msgs[1]["content"]
        assert msgs[2]["role"] == "assistant"
        assert "Sandbox" in msgs[2]["content"]
        assert msgs[3]["role"] == "user"
        assert "1" in msgs[3]["content"]

    def test_history_excluded_from_system_prompt(self) -> None:
        llm = FakeLLM()
        runner = ChatRunner(llm=llm)
        ctx = AssembledContext(
            symbiote_id="sym-1",
            session_id="sess-1",
            user_input="1",
            extra_context={
                "conversation_history": "Usuário: hello\n\nClark: hi there",
                "page_url": "/inbox",
            },
        )

        runner.run(ctx)

        system = llm.last_messages[0]["content"]
        assert "conversation_history" not in system
        assert "page_url" in system  # other extra_context still there

    def test_no_history_no_extra_messages(self) -> None:
        llm = FakeLLM()
        runner = ChatRunner(llm=llm)
        ctx = AssembledContext(
            symbiote_id="sym-1",
            session_id="sess-1",
            user_input="hello",
            extra_context={"page_url": "/inbox"},
        )

        runner.run(ctx)

        # system + user only
        assert len(llm.last_messages) == 2

    def test_working_memory_takes_precedence(self) -> None:
        """When working_memory is provided, conversation_history is ignored."""
        llm = FakeLLM()
        wm = WorkingMemory(session_id="sess-1")
        wm.update_message(Message(session_id="sess-1", role="user", content="previous"))
        wm.update_message(Message(session_id="sess-1", role="assistant", content="response"))
        runner = ChatRunner(llm=llm, working_memory=wm)
        ctx = AssembledContext(
            symbiote_id="sym-1",
            session_id="sess-1",
            user_input="new message",
            extra_context={
                "conversation_history": "Usuário: should be ignored\n\nClark: ignored too",
            },
        )

        runner.run(ctx)

        msgs = llm.last_messages
        # system + 2 from working_memory + user
        assert len(msgs) == 4
        assert msgs[1]["content"] == "previous"
        assert msgs[2]["content"] == "response"
