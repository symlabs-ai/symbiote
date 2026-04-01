"""Tests for B-68 memory/knowledge on-demand mode."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.context import AssembledContext, ContextAssembler
from symbiote.core.identity import IdentityManager
from symbiote.core.models import EnvironmentConfig, MemoryEntry
from symbiote.environment.manager import EnvironmentManager
from symbiote.environment.policies import PolicyGate
from symbiote.environment.tools import ToolGateway
from symbiote.knowledge.models import KnowledgeEntry
from symbiote.knowledge.service import KnowledgeService
from symbiote.memory.store import MemoryStore
from symbiote.runners.chat import ChatRunner


@pytest.fixture()
def storage(tmp_path: Path):
    db = SQLiteAdapter(tmp_path / "test.db", check_same_thread=False)
    db.init_schema()
    return db


@pytest.fixture()
def identity(storage):
    return IdentityManager(storage)


@pytest.fixture()
def memory(storage):
    return MemoryStore(storage)


@pytest.fixture()
def knowledge(storage):
    return KnowledgeService(storage)


@pytest.fixture()
def environment(storage):
    return EnvironmentManager(storage)


@pytest.fixture()
def policy_gate(environment, storage):
    return PolicyGate(environment, storage)


@pytest.fixture()
def tool_gateway(policy_gate):
    return ToolGateway(policy_gate)


@pytest.fixture()
def symbiote(identity):
    return identity.create(name="test-agent", role="assistant")


# ── Default is packed ────────────────────────────────────────────────────────


def test_default_context_mode_is_packed():
    cfg = EnvironmentConfig(symbiote_id="s1")
    assert cfg.context_mode == "packed"


# ── EnvironmentConfig round-trip ─────────────────────────────────────────────


def test_environment_config_roundtrip_context_mode(environment, symbiote):
    # Create with on_demand
    cfg = environment.configure(
        symbiote_id=symbiote.id, context_mode="on_demand"
    )
    assert cfg.context_mode == "on_demand"

    # Read back
    loaded = environment.get_config(symbiote.id)
    assert loaded is not None
    assert loaded.context_mode == "on_demand"

    # Update to packed
    cfg2 = environment.configure(
        symbiote_id=symbiote.id, context_mode="packed"
    )
    assert cfg2.context_mode == "packed"

    loaded2 = environment.get_config(symbiote.id)
    assert loaded2 is not None
    assert loaded2.context_mode == "packed"


def test_get_context_mode_default(environment, symbiote):
    """get_context_mode returns 'packed' when no config exists."""
    assert environment.get_context_mode(symbiote.id) == "packed"


def test_get_context_mode_on_demand(environment, symbiote):
    environment.configure(symbiote_id=symbiote.id, context_mode="on_demand")
    assert environment.get_context_mode(symbiote.id) == "on_demand"


# ── Packed mode: memories and knowledge injected ─────────────────────────────


def test_packed_mode_injects_memories_and_knowledge(
    identity, memory, knowledge, environment, tool_gateway, symbiote
):
    # Seed data
    memory.store(
        MemoryEntry(
            symbiote_id=symbiote.id,
            type="factual",
            scope="global",
            content="The sky is blue",
            importance=0.9,
            source="user",
        )
    )
    knowledge.register_source(
        symbiote_id=symbiote.id,
        name="Sky Facts",
        content="The sky appears blue due to Rayleigh scattering",
    )

    # Default config (packed)
    environment.configure(symbiote_id=symbiote.id)

    assembler = ContextAssembler(
        identity=identity,
        memory=memory,
        knowledge=knowledge,
        context_budget=16000,
        tool_gateway=tool_gateway,
        environment=environment,
    )

    ctx = assembler.build(
        session_id="s1",
        symbiote_id=symbiote.id,
        user_input="sky",
    )

    assert ctx.context_mode == "packed"
    assert len(ctx.relevant_memories) > 0
    assert len(ctx.relevant_knowledge) > 0


# ── On-demand mode: memories and knowledge NOT injected ──────────────────────


def test_on_demand_mode_skips_memories_and_knowledge(
    identity, memory, knowledge, environment, tool_gateway, symbiote
):
    # Seed data
    memory.store(
        MemoryEntry(
            symbiote_id=symbiote.id,
            type="factual",
            scope="global",
            content="The sky is blue",
            importance=0.9,
            source="user",
        )
    )
    knowledge.register_source(
        symbiote_id=symbiote.id,
        name="Weather Facts",
        content="Weather is the state of the atmosphere",
    )

    # Configure on_demand
    environment.configure(symbiote_id=symbiote.id, context_mode="on_demand")

    assembler = ContextAssembler(
        identity=identity,
        memory=memory,
        knowledge=knowledge,
        context_budget=16000,
        tool_gateway=tool_gateway,
        environment=environment,
    )

    ctx = assembler.build(
        session_id="s1",
        symbiote_id=symbiote.id,
        user_input="tell me about the sky",
    )

    assert ctx.context_mode == "on_demand"
    assert ctx.relevant_memories == []
    assert ctx.relevant_knowledge == []


# ── Tool registration ────────────────────────────────────────────────────────


def test_search_memories_tool_registered(tool_gateway, memory, knowledge):
    tool_gateway.register_memory_tools(memory, knowledge)
    assert tool_gateway.has_tool("search_memories")
    desc = tool_gateway.get_descriptor("search_memories")
    assert desc is not None
    assert desc.handler_type == "builtin"


def test_search_knowledge_tool_registered(tool_gateway, memory, knowledge):
    tool_gateway.register_memory_tools(memory, knowledge)
    assert tool_gateway.has_tool("search_knowledge")
    desc = tool_gateway.get_descriptor("search_knowledge")
    assert desc is not None
    assert desc.handler_type == "builtin"


# ── Tool execution ───────────────────────────────────────────────────────────


def test_search_memories_tool_callable(
    tool_gateway, environment, memory, knowledge, symbiote, storage
):
    tool_gateway.register_memory_tools(memory, knowledge)
    # Allow the tool for this symbiote
    environment.configure(
        symbiote_id=symbiote.id,
        tools=["search_memories"],
    )

    # Seed a memory
    memory.store(
        MemoryEntry(
            symbiote_id=symbiote.id,
            type="factual",
            scope="global",
            content="Python is a programming language",
            importance=0.8,
            source="user",
        )
    )

    result = tool_gateway.execute(
        symbiote_id=symbiote.id,
        session_id=None,
        tool_id="search_memories",
        params={"query": "Python", "limit": 5},
    )

    assert result.success
    assert isinstance(result.output, list)
    assert len(result.output) > 0
    assert result.output[0]["content"] == "Python is a programming language"


def test_search_knowledge_tool_callable(
    tool_gateway, environment, memory, knowledge, symbiote, storage
):
    tool_gateway.register_memory_tools(memory, knowledge)
    environment.configure(
        symbiote_id=symbiote.id,
        tools=["search_knowledge"],
    )

    knowledge.register_source(
        symbiote_id=symbiote.id,
        name="Python Guide",
        content="Python is great for scripting",
    )

    result = tool_gateway.execute(
        symbiote_id=symbiote.id,
        session_id=None,
        tool_id="search_knowledge",
        params={"symbiote_id": symbiote.id, "query": "Python", "limit": 5},
    )

    assert result.success
    assert isinstance(result.output, list)
    assert len(result.output) > 0
    assert result.output[0]["name"] == "Python Guide"


# ── System prompt includes on-demand instruction ────────────────────────────


def test_on_demand_adds_instruction_to_system_prompt():
    """When context_mode is on_demand and no memories/knowledge, the system
    prompt should include an instruction about search tools."""
    llm = MagicMock()
    runner = ChatRunner(llm=llm)

    context = AssembledContext(
        symbiote_id="s1",
        session_id="sess1",
        persona={"role": "helper"},
        relevant_memories=[],
        relevant_knowledge=[],
        available_tools=[],
        context_mode="on_demand",
        user_input="hello",
    )

    system = runner._build_system(context)
    assert "search_memories" in system
    assert "search_knowledge" in system


def test_packed_mode_no_on_demand_instruction():
    """Packed mode should NOT include the on-demand instruction."""
    llm = MagicMock()
    runner = ChatRunner(llm=llm)

    context = AssembledContext(
        symbiote_id="s1",
        session_id="sess1",
        persona={"role": "helper"},
        relevant_memories=[{"content": "some memory", "type": "factual", "importance": 0.5}],
        relevant_knowledge=[],
        available_tools=[],
        context_mode="packed",
        user_input="hello",
    )

    system = runner._build_system(context)
    assert "search_memories and search_knowledge tools" not in system
