"""Tests for CapabilitySurface — T-19 TDD RED phase."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.capabilities import CapabilityError, CapabilitySurface
from symbiote.core.context import ContextAssembler
from symbiote.core.identity import IdentityManager
from symbiote.core.models import MemoryEntry
from symbiote.core.session import SessionManager
from symbiote.knowledge.service import KnowledgeService
from symbiote.memory.store import MemoryStore
from symbiote.runners.base import RunnerRegistry, RunResult
from symbiote.runners.chat import ChatRunner

# ── Fake LLM ────────────────────────────────────────────────────────────────


class FakeLLM:
    """Implements LLMPort, returns a canned response."""

    def __init__(self, response: str = "Hello from LLM") -> None:
        self.response = response
        self.last_messages: list[dict] | None = None
        self.call_count: int = 0

    def complete(self, messages: list[dict], config: dict | None = None) -> str:
        self.last_messages = messages
        self.call_count += 1
        return self.response


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "cap_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def identity(adapter: SQLiteAdapter) -> IdentityManager:
    return IdentityManager(storage=adapter)


@pytest.fixture()
def sessions(adapter: SQLiteAdapter) -> SessionManager:
    return SessionManager(storage=adapter)


@pytest.fixture()
def memory(adapter: SQLiteAdapter) -> MemoryStore:
    return MemoryStore(storage=adapter)


@pytest.fixture()
def knowledge(adapter: SQLiteAdapter) -> KnowledgeService:
    return KnowledgeService(storage=adapter)


@pytest.fixture()
def context_assembler(
    identity: IdentityManager,
    memory: MemoryStore,
    knowledge: KnowledgeService,
) -> ContextAssembler:
    return ContextAssembler(
        identity=identity,
        memory=memory,
        knowledge=knowledge,
        context_budget=4000,
    )


@pytest.fixture()
def fake_llm() -> FakeLLM:
    return FakeLLM(response="LLM says hello")


@pytest.fixture()
def runner_registry(fake_llm: FakeLLM) -> RunnerRegistry:
    registry = RunnerRegistry()
    registry.register(ChatRunner(llm=fake_llm))
    return registry


@pytest.fixture()
def symbiote_id(identity: IdentityManager) -> str:
    sym = identity.create(
        name="CapBot",
        role="assistant",
        persona={"tone": "friendly", "expertise": "python"},
    )
    return sym.id


@pytest.fixture()
def session_id(sessions: SessionManager, symbiote_id: str) -> str:
    session = sessions.start(symbiote_id=symbiote_id, goal="test capabilities")
    return session.id


@pytest.fixture()
def surface(
    identity: IdentityManager,
    sessions: SessionManager,
    memory: MemoryStore,
    knowledge: KnowledgeService,
    context_assembler: ContextAssembler,
    runner_registry: RunnerRegistry,
) -> CapabilitySurface:
    return CapabilitySurface(
        identity=identity,
        sessions=sessions,
        memory=memory,
        knowledge=knowledge,
        context_assembler=context_assembler,
        runner_registry=runner_registry,
    )


# ── learn tests ─────────────────────────────────────────────────────────────


class TestLearn:
    def test_learn_persists_memory_entry(
        self,
        surface: CapabilitySurface,
        memory: MemoryStore,
        symbiote_id: str,
        session_id: str,
    ) -> None:
        entry = surface.learn(
            symbiote_id=symbiote_id,
            session_id=session_id,
            content="Python uses indentation for blocks",
            fact_type="factual",
            importance=0.9,
        )

        assert isinstance(entry, MemoryEntry)
        assert entry.content == "Python uses indentation for blocks"
        assert entry.type == "factual"
        assert entry.importance == 0.9
        assert entry.symbiote_id == symbiote_id
        assert entry.session_id == session_id

        # Verify it was actually persisted
        stored = memory.get(entry.id)
        assert stored is not None
        assert stored.content == "Python uses indentation for blocks"

    def test_learn_defaults(
        self,
        surface: CapabilitySurface,
        symbiote_id: str,
        session_id: str,
    ) -> None:
        entry = surface.learn(
            symbiote_id=symbiote_id,
            session_id=session_id,
            content="Default importance fact",
        )

        assert entry.type == "factual"
        assert entry.importance == 0.7

    def test_learn_with_different_type(
        self,
        surface: CapabilitySurface,
        symbiote_id: str,
        session_id: str,
    ) -> None:
        entry = surface.learn(
            symbiote_id=symbiote_id,
            session_id=session_id,
            content="User prefers dark mode",
            fact_type="preference",
            importance=0.8,
        )

        assert entry.type == "preference"


# ── teach tests ─────────────────────────────────────────────────────────────


class TestTeach:
    def test_teach_returns_formatted_text(
        self,
        surface: CapabilitySurface,
        knowledge: KnowledgeService,
        memory: MemoryStore,
        symbiote_id: str,
        session_id: str,
    ) -> None:
        # Seed knowledge
        knowledge.register_source(
            symbiote_id=symbiote_id,
            name="Python Basics",
            content="Python is a high-level programming language.",
        )
        # Seed a relevant memory
        memory.store(
            MemoryEntry(
                symbiote_id=symbiote_id,
                session_id=session_id,
                type="factual",
                scope="global",
                content="Python uses indentation for blocks",
                importance=0.8,
                source="user",
            )
        )

        result = surface.teach(
            symbiote_id=symbiote_id,
            session_id=session_id,
            query="Python",
        )

        assert isinstance(result, str)
        assert len(result) > 0
        # Should include knowledge content
        assert "Python" in result

    def test_teach_with_no_data_returns_message(
        self,
        surface: CapabilitySurface,
        symbiote_id: str,
        session_id: str,
    ) -> None:
        result = surface.teach(
            symbiote_id=symbiote_id,
            session_id=session_id,
            query="completely unknown topic xyz123",
        )

        assert isinstance(result, str)
        assert len(result) > 0


# ── chat tests ──────────────────────────────────────────────────────────────


class TestChat:
    def test_chat_returns_llm_response(
        self,
        surface: CapabilitySurface,
        fake_llm: FakeLLM,
        symbiote_id: str,
        session_id: str,
    ) -> None:
        response = surface.chat(
            symbiote_id=symbiote_id,
            session_id=session_id,
            content="Hello, how are you?",
        )

        assert response == "LLM says hello"
        assert fake_llm.call_count == 1

    def test_chat_uses_context_assembler(
        self,
        surface: CapabilitySurface,
        fake_llm: FakeLLM,
        symbiote_id: str,
        session_id: str,
    ) -> None:
        surface.chat(
            symbiote_id=symbiote_id,
            session_id=session_id,
            content="Tell me about Python",
        )

        # The FakeLLM should have received messages built by ContextAssembler + ChatRunner
        assert fake_llm.last_messages is not None
        assert len(fake_llm.last_messages) >= 2  # at least system + user
        assert fake_llm.last_messages[-1]["role"] == "user"
        assert "Tell me about Python" in fake_llm.last_messages[-1]["content"]

    def test_chat_with_no_chat_runner_raises(
        self,
        identity: IdentityManager,
        sessions: SessionManager,
        memory: MemoryStore,
        knowledge: KnowledgeService,
        context_assembler: ContextAssembler,
        symbiote_id: str,
        session_id: str,
    ) -> None:
        # Registry with no ChatRunner
        empty_registry = RunnerRegistry()
        surf = CapabilitySurface(
            identity=identity,
            sessions=sessions,
            memory=memory,
            knowledge=knowledge,
            context_assembler=context_assembler,
            runner_registry=empty_registry,
        )

        with pytest.raises(CapabilityError, match="[Nn]o runner"):
            surf.chat(
                symbiote_id=symbiote_id,
                session_id=session_id,
                content="Hello",
            )


# ── work tests ──────────────────────────────────────────────────────────────


class TestWork:
    def test_work_selects_runner_and_returns_result(
        self,
        surface: CapabilitySurface,
        symbiote_id: str,
        session_id: str,
    ) -> None:
        result = surface.work(
            symbiote_id=symbiote_id,
            session_id=session_id,
            task="chat: help me with code",
        )

        assert isinstance(result, dict)
        assert "success" in result
        assert result["success"] is True

    def test_work_with_unknown_intent_raises(
        self,
        surface: CapabilitySurface,
        symbiote_id: str,
        session_id: str,
    ) -> None:
        with pytest.raises(CapabilityError, match="[Nn]o runner"):
            surface.work(
                symbiote_id=symbiote_id,
                session_id=session_id,
                task="completely_unknown_intent: do something",
            )


# ── show tests ──────────────────────────────────────────────────────────────


class TestShow:
    def test_show_returns_markdown(
        self,
        surface: CapabilitySurface,
        memory: MemoryStore,
        knowledge: KnowledgeService,
        sessions: SessionManager,
        symbiote_id: str,
        session_id: str,
    ) -> None:
        # Seed some data
        memory.store(
            MemoryEntry(
                symbiote_id=symbiote_id,
                session_id=session_id,
                type="factual",
                scope="global",
                content="Important show fact",
                importance=0.9,
                source="user",
            )
        )
        sessions.add_message(session_id, "user", "show me stuff")

        result = surface.show(
            symbiote_id=symbiote_id,
            session_id=session_id,
            query="show",
        )

        assert isinstance(result, str)
        # Should be Markdown-ish
        assert "#" in result

    def test_show_with_export_fn(
        self,
        identity: IdentityManager,
        sessions: SessionManager,
        memory: MemoryStore,
        knowledge: KnowledgeService,
        context_assembler: ContextAssembler,
        runner_registry: RunnerRegistry,
        symbiote_id: str,
        session_id: str,
    ) -> None:
        export_calls: list[tuple[str, str]] = []

        def fake_export(fmt: str, content: str) -> str:
            export_calls.append((fmt, content))
            return f"exported:{fmt}"

        surf = CapabilitySurface(
            identity=identity,
            sessions=sessions,
            memory=memory,
            knowledge=knowledge,
            context_assembler=context_assembler,
            runner_registry=runner_registry,
            export_fn=fake_export,
        )

        result = surf.show(
            symbiote_id=symbiote_id,
            session_id=session_id,
            query="anything",
        )

        # Should still return string (Markdown by default, export_fn is optional usage)
        assert isinstance(result, str)


# ── reflect tests ───────────────────────────────────────────────────────────


class TestReflect:
    def test_reflect_returns_summary_dict(
        self,
        surface: CapabilitySurface,
        sessions: SessionManager,
        symbiote_id: str,
        session_id: str,
    ) -> None:
        # Add some messages to the session
        sessions.add_message(session_id, "user", "Hello there")
        sessions.add_message(session_id, "assistant", "Hi! How can I help?")
        sessions.add_message(session_id, "user", "Tell me about Python")

        result = surface.reflect(
            symbiote_id=symbiote_id,
            session_id=session_id,
        )

        assert isinstance(result, dict)
        assert "session_id" in result
        assert result["session_id"] == session_id
        assert "message_count" in result
        assert result["message_count"] >= 3
        assert "summary" in result

    def test_reflect_empty_session(
        self,
        surface: CapabilitySurface,
        symbiote_id: str,
        session_id: str,
    ) -> None:
        result = surface.reflect(
            symbiote_id=symbiote_id,
            session_id=session_id,
        )

        assert isinstance(result, dict)
        assert result["message_count"] == 0
