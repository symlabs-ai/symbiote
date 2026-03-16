"""Tests for ContextAssembler — T-11."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.context import AssembledContext, ContextAssembler, ContextInspection
from symbiote.core.exceptions import EntityNotFoundError
from symbiote.core.identity import IdentityManager
from symbiote.core.models import Decision, MemoryEntry, Message
from symbiote.knowledge.service import KnowledgeService
from symbiote.memory.store import MemoryStore
from symbiote.memory.working import WorkingMemory


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "context_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def identity(adapter: SQLiteAdapter) -> IdentityManager:
    return IdentityManager(storage=adapter)


@pytest.fixture()
def memory(adapter: SQLiteAdapter) -> MemoryStore:
    return MemoryStore(storage=adapter)


@pytest.fixture()
def knowledge(adapter: SQLiteAdapter) -> KnowledgeService:
    return KnowledgeService(storage=adapter)


@pytest.fixture()
def symbiote_id(identity: IdentityManager) -> str:
    sym = identity.create(
        name="TestBot",
        role="assistant",
        persona={"tone": "friendly", "expertise": "python"},
    )
    return sym.id


@pytest.fixture()
def session_id(adapter: SQLiteAdapter, symbiote_id: str) -> str:
    """Create a session row and return its ID."""
    from symbiote.core.session import SessionManager

    mgr = SessionManager(storage=adapter)
    session = mgr.start(symbiote_id=symbiote_id, goal="test goal")
    return session.id


@pytest.fixture()
def assembler(
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


# ── Build with full data ──────────────────────────────────────────────────


class TestBuildFullData:
    def test_build_returns_assembled_context_with_all_sections(
        self,
        assembler: ContextAssembler,
        memory: MemoryStore,
        knowledge: KnowledgeService,
        symbiote_id: str,
        session_id: str,
    ) -> None:
        # Seed memories
        memory.store(
            MemoryEntry(
                symbiote_id=symbiote_id,
                session_id=session_id,
                type="factual",
                scope="session",
                content="User prefers dark mode.",
                importance=0.8,
                source="user",
            )
        )
        # Seed knowledge
        knowledge.register_source(
            symbiote_id=symbiote_id,
            name="Dark Mode Guide",
            content="Instructions for dark mode configuration.",
        )

        wm = WorkingMemory(session_id=session_id)
        wm.update_goal("Help with dark mode")
        wm.update_message(
            Message(session_id=session_id, role="user", content="dark mode help")
        )

        ctx = assembler.build(
            session_id=session_id,
            symbiote_id=symbiote_id,
            user_input="dark mode",
            working_memory=wm,
        )

        assert isinstance(ctx, AssembledContext)
        assert ctx.symbiote_id == symbiote_id
        assert ctx.session_id == session_id
        assert ctx.user_input == "dark mode"
        assert ctx.persona is not None
        assert ctx.persona["tone"] == "friendly"
        assert ctx.working_memory_snapshot is not None
        assert ctx.working_memory_snapshot["current_goal"] == "Help with dark mode"
        assert len(ctx.relevant_memories) >= 1
        assert len(ctx.relevant_knowledge) >= 1
        assert ctx.total_tokens_estimate > 0


# ── Build with empty data ─────────────────────────────────────────────────


class TestBuildEmptyData:
    def test_build_with_no_memories_or_knowledge(
        self,
        assembler: ContextAssembler,
        symbiote_id: str,
        session_id: str,
    ) -> None:
        ctx = assembler.build(
            session_id=session_id,
            symbiote_id=symbiote_id,
            user_input="something unrelated",
        )

        assert isinstance(ctx, AssembledContext)
        assert ctx.relevant_memories == []
        assert ctx.relevant_knowledge == []
        assert ctx.working_memory_snapshot is None
        assert ctx.persona is not None  # persona still comes from identity

    def test_build_with_nonexistent_symbiote_raises(
        self,
        assembler: ContextAssembler,
        session_id: str,
    ) -> None:
        with pytest.raises(EntityNotFoundError, match="not found"):
            assembler.build(
                session_id=session_id,
                symbiote_id="nonexistent-id",
                user_input="hello",
            )


# ── Budget enforcement ────────────────────────────────────────────────────


class TestBudgetEnforcement:
    def test_build_trims_least_important_when_over_budget(
        self,
        identity: IdentityManager,
        memory: MemoryStore,
        knowledge: KnowledgeService,
    ) -> None:
        sym = identity.create(name="SmallBot", role="assistant", persona={"a": "b"})
        from symbiote.core.session import SessionManager

        adapter = identity._storage
        mgr = SessionManager(storage=adapter)
        session = mgr.start(symbiote_id=sym.id, goal="test")

        # Use a very small budget to force trimming
        small_assembler = ContextAssembler(
            identity=identity,
            memory=memory,
            knowledge=knowledge,
            context_budget=100,  # very small budget
        )

        # Seed many memories with different importance
        for i in range(10):
            memory.store(
                MemoryEntry(
                    symbiote_id=sym.id,
                    session_id=session.id,
                    type="factual",
                    scope="session",
                    content=f"Memory item number {i} with some searchable content for testing",
                    importance=round(i * 0.1, 1),  # 0.0 to 0.9
                    source="user",
                )
            )

        # Seed knowledge
        for i in range(5):
            knowledge.register_source(
                symbiote_id=sym.id,
                name=f"Knowledge {i} searchable",
                content=f"Knowledge content {i} with searchable material for testing",
            )

        ctx = small_assembler.build(
            session_id=session.id,
            symbiote_id=sym.id,
            user_input="searchable",
        )

        assert isinstance(ctx, AssembledContext)
        # Total tokens should be within budget (or at least trimmed)
        assert ctx.total_tokens_estimate <= 100

        # If memories were trimmed, the remaining should be the most important ones
        if len(ctx.relevant_memories) > 0:
            importances = [m["importance"] for m in ctx.relevant_memories]
            # Should be sorted descending (most important first)
            assert importances == sorted(importances, reverse=True)


# ── Persona inclusion ────────────────────────────────────────────────────


class TestPersonaInclusion:
    def test_build_includes_persona_from_identity(
        self,
        assembler: ContextAssembler,
        symbiote_id: str,
        session_id: str,
    ) -> None:
        ctx = assembler.build(
            session_id=session_id,
            symbiote_id=symbiote_id,
            user_input="hello",
        )
        assert ctx.persona is not None
        assert ctx.persona["tone"] == "friendly"
        assert ctx.persona["expertise"] == "python"


# ── Working memory snapshot ──────────────────────────────────────────────


class TestWorkingMemorySnapshot:
    def test_build_includes_working_memory_snapshot(
        self,
        assembler: ContextAssembler,
        symbiote_id: str,
        session_id: str,
    ) -> None:
        wm = WorkingMemory(session_id=session_id)
        wm.update_goal("Build a CLI")
        wm.update_plan("Step 1: argparse")
        wm.add_active_file("main.py")
        wm.add_decision(
            Decision(session_id=session_id, title="Use argparse")
        )

        ctx = assembler.build(
            session_id=session_id,
            symbiote_id=symbiote_id,
            user_input="hello",
            working_memory=wm,
        )

        snap = ctx.working_memory_snapshot
        assert snap is not None
        assert snap["current_goal"] == "Build a CLI"
        assert snap["active_plan"] == "Step 1: argparse"
        assert "main.py" in snap["active_files"]

    def test_build_without_working_memory(
        self,
        assembler: ContextAssembler,
        symbiote_id: str,
        session_id: str,
    ) -> None:
        ctx = assembler.build(
            session_id=session_id,
            symbiote_id=symbiote_id,
            user_input="hello",
        )
        assert ctx.working_memory_snapshot is None


# ── Inspect ──────────────────────────────────────────────────────────────


class TestInspect:
    def test_inspect_returns_correct_counts(
        self,
        assembler: ContextAssembler,
        memory: MemoryStore,
        knowledge: KnowledgeService,
        symbiote_id: str,
        session_id: str,
    ) -> None:
        memory.store(
            MemoryEntry(
                symbiote_id=symbiote_id,
                session_id=session_id,
                type="factual",
                scope="session",
                content="Some inspectable memory content",
                importance=0.7,
                source="user",
            )
        )
        knowledge.register_source(
            symbiote_id=symbiote_id,
            name="Inspectable Doc",
            content="Inspectable knowledge content",
        )

        ctx = assembler.build(
            session_id=session_id,
            symbiote_id=symbiote_id,
            user_input="inspectable",
        )
        inspection = assembler.inspect(ctx)

        assert isinstance(inspection, ContextInspection)
        assert inspection.included_memories == len(ctx.relevant_memories)
        assert inspection.included_knowledge == len(ctx.relevant_knowledge)
        assert inspection.total_tokens_estimate == ctx.total_tokens_estimate
        assert inspection.budget == 4000
        assert inspection.within_budget is True

    def test_inspect_within_budget_flag(
        self,
        identity: IdentityManager,
        memory: MemoryStore,
        knowledge: KnowledgeService,
    ) -> None:
        """Inspect should report within_budget=True when context fits."""
        sym = identity.create(name="InspBot", role="assistant")
        from symbiote.core.session import SessionManager

        adapter = identity._storage
        mgr = SessionManager(storage=adapter)
        session = mgr.start(symbiote_id=sym.id, goal="test")

        assembler = ContextAssembler(
            identity=identity,
            memory=memory,
            knowledge=knowledge,
            context_budget=10000,
        )

        ctx = assembler.build(
            session_id=session.id,
            symbiote_id=sym.id,
            user_input="hello",
        )
        inspection = assembler.inspect(ctx)
        assert inspection.within_budget is True
        assert inspection.total_tokens_estimate <= inspection.budget


# ── Token estimation ─────────────────────────────────────────────────────


class TestTokenEstimation:
    def test_estimate_tokens_heuristic(
        self,
        assembler: ContextAssembler,
    ) -> None:
        text = "a" * 400  # 400 chars -> ~100 tokens
        estimate = assembler._estimate_tokens(text)
        assert estimate == 100

    def test_estimate_tokens_empty_string(
        self,
        assembler: ContextAssembler,
    ) -> None:
        assert assembler._estimate_tokens("") == 0

    def test_estimate_tokens_short_string(
        self,
        assembler: ContextAssembler,
    ) -> None:
        # 3 chars -> 3//4 = 0
        assert assembler._estimate_tokens("abc") == 0
        # 4 chars -> 4//4 = 1
        assert assembler._estimate_tokens("abcd") == 1
