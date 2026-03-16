"""Tests for extra_context injection in context assembly and chat runner."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.context import AssembledContext, ContextAssembler
from symbiote.core.identity import IdentityManager
from symbiote.core.ports import LLMPort
from symbiote.knowledge.service import KnowledgeService
from symbiote.memory.store import MemoryStore
from symbiote.runners.chat import ChatRunner


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "extra_ctx_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name="CtxBot", role="assistant")
    return sym.id


@pytest.fixture()
def session_id(adapter: SQLiteAdapter, symbiote_id: str) -> str:
    from symbiote.core.session import SessionManager

    mgr = SessionManager(storage=adapter)
    sess = mgr.start(symbiote_id=symbiote_id)
    return sess.id


class TestAssembledContextExtraField:
    def test_default_none(self) -> None:
        ctx = AssembledContext(
            symbiote_id="s", session_id="ss", user_input="hi"
        )
        assert ctx.extra_context is None

    def test_set_extra_context(self) -> None:
        ctx = AssembledContext(
            symbiote_id="s",
            session_id="ss",
            user_input="hi",
            extra_context={"page_url": "/compose", "page_content": "Draft text"},
        )
        assert ctx.extra_context["page_url"] == "/compose"


class TestContextAssemblerExtraContext:
    def test_build_passes_extra_context(
        self, adapter: SQLiteAdapter, symbiote_id: str, session_id: str
    ) -> None:
        assembler = ContextAssembler(
            identity=IdentityManager(storage=adapter),
            memory=MemoryStore(storage=adapter),
            knowledge=KnowledgeService(storage=adapter),
        )
        ctx = assembler.build(
            session_id=session_id,
            symbiote_id=symbiote_id,
            user_input="help",
            extra_context={"page": "article about AI"},
        )
        assert ctx.extra_context == {"page": "article about AI"}

    def test_build_without_extra_context(
        self, adapter: SQLiteAdapter, symbiote_id: str, session_id: str
    ) -> None:
        assembler = ContextAssembler(
            identity=IdentityManager(storage=adapter),
            memory=MemoryStore(storage=adapter),
            knowledge=KnowledgeService(storage=adapter),
        )
        ctx = assembler.build(
            session_id=session_id,
            symbiote_id=symbiote_id,
            user_input="help",
        )
        assert ctx.extra_context is None


class TestChatRunnerExtraContextInPrompt:
    def test_extra_context_appears_in_system_prompt(self) -> None:
        messages_seen: list[list[dict]] = []

        class CaptureLLM:
            def complete(self, messages: list[dict], config: dict | None = None) -> str:
                messages_seen.append(messages)
                return "ok"

        runner = ChatRunner(CaptureLLM())
        ctx = AssembledContext(
            symbiote_id="s1",
            session_id="ss1",
            user_input="suggest intro",
            extra_context={
                "page_url": "/compose/draft-123",
                "draft_content": "The economy is growing...",
            },
        )
        runner.run(ctx)

        system = messages_seen[0][0]["content"]
        assert "## Context" in system
        assert "page_url" in system
        assert "/compose/draft-123" in system
        assert "draft_content" in system
        assert "The economy is growing..." in system

    def test_no_extra_context_no_section(self) -> None:
        messages_seen: list[list[dict]] = []

        class CaptureLLM:
            def complete(self, messages: list[dict], config: dict | None = None) -> str:
                messages_seen.append(messages)
                return "ok"

        runner = ChatRunner(CaptureLLM())
        ctx = AssembledContext(
            symbiote_id="s1",
            session_id="ss1",
            user_input="hello",
        )
        runner.run(ctx)

        system = messages_seen[0][0]["content"]
        assert "## Context" not in system
