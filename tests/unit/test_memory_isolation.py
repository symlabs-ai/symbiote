"""Regression: memories never cross symbiote boundaries (report item S-1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.core.exceptions import EntityNotFoundError
from symbiote.core.models import MemoryEntry
from tests.support import ScriptedLLM, make_kernel

SECRET = "Acme customer token is ACME-9931"


def _memory(symbiote_id: str, content: str, **kw) -> MemoryEntry:
    kw.setdefault("type", "factual")
    kw.setdefault("importance", 0.9)
    return MemoryEntry(
        symbiote_id=symbiote_id, scope="global", source="user", content=content, **kw
    )


@pytest.fixture()
def two_symbiotes(tmp_path: Path):
    llm = ScriptedLLM("ok")
    kernel = make_kernel(tmp_path, llm)
    a = kernel.create_symbiote(name="A", role="assistant")
    b = kernel.create_symbiote(name="B", role="assistant")
    secret_id = kernel._memory.store(_memory(a.id, SECRET))
    yield kernel, llm, a, b, secret_id
    kernel.shutdown()


@pytest.mark.parametrize("message", ["customer token", "%", "_"])
def test_other_symbiote_memory_never_reaches_prompt(two_symbiotes, message) -> None:
    kernel, llm, _a, b, _ = two_symbiotes
    session = kernel.start_session(b.id)

    kernel.message(session.id, message)

    assert "ACME-9931" not in llm.all_prompts_text()


def test_own_memory_still_reaches_prompt(two_symbiotes) -> None:
    kernel, llm, a, _b, _ = two_symbiotes
    session = kernel.start_session(a.id)

    kernel.message(session.id, "customer token")

    assert "ACME-9931" in llm.all_prompts_text()


def test_store_search_requires_and_applies_symbiote(two_symbiotes) -> None:
    kernel, _llm, a, b, _ = two_symbiotes
    memory = kernel._memory

    with pytest.raises(TypeError):
        memory.search("Acme")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        memory.get_relevant("Acme")  # type: ignore[call-arg]

    assert memory.search("Acme", symbiote_id=b.id) == []
    assert [m.content for m in memory.search("Acme", symbiote_id=a.id)] == [SECRET]
    assert memory.get_relevant("Acme", symbiote_id=b.id) == []


def test_like_wildcards_are_literal(two_symbiotes) -> None:
    kernel, _llm, a, _b, _ = two_symbiotes
    kernel._memory.store(_memory(a.id, "discount is 50% today"))

    assert [m.content for m in kernel._memory.search("50%", symbiote_id=a.id)] == [
        "discount is 50% today"
    ]
    assert kernel._memory.search("%", symbiote_id=a.id)[0].content == "discount is 50% today"
    assert kernel._memory.search("_", symbiote_id=a.id) == []


def test_search_memories_tool_is_scoped_to_caller(two_symbiotes) -> None:
    kernel, _llm, a, b, _ = two_symbiotes
    kernel._memory.store(_memory(b.id, "B likes tea"))
    kernel.environment.configure(symbiote_id=b.id, tools=["search_memories"])
    session = kernel.start_session(b.id)

    leaked = kernel.tool_gateway.execute(
        b.id, session.id, "search_memories", {"query": "Acme"}
    )
    own = kernel.tool_gateway.execute(
        b.id, session.id, "search_memories", {"query": "tea"}
    )

    assert leaked.success and leaked.output == []
    assert [m["content"] for m in own.output] == ["B likes tea"]


def test_search_knowledge_ignores_symbiote_id_param(two_symbiotes) -> None:
    kernel, _llm, a, b, _ = two_symbiotes
    kernel._knowledge.register_source(symbiote_id=a.id, name="a-doc", content="vault procedures for A")
    kernel._knowledge.register_source(symbiote_id=b.id, name="b-doc", content="vault notes for B")
    kernel.environment.configure(symbiote_id=b.id, tools=["search_knowledge"])
    session = kernel.start_session(b.id)

    result = kernel.tool_gateway.execute(
        b.id, session.id, "search_knowledge", {"query": "vault", "symbiote_id": a.id}
    )

    assert result.success
    assert [k["name"] for k in result.output] == ["b-doc"]


def test_update_and_deactivate_respect_symbiote(two_symbiotes) -> None:
    kernel, _llm, a, b, secret_id = two_symbiotes
    memory = kernel._memory

    assert memory.get(secret_id, symbiote_id=b.id) is None
    assert memory.update(secret_id, content="overwritten", symbiote_id=b.id) is False
    with pytest.raises(EntityNotFoundError):
        memory.deactivate(secret_id, symbiote_id=b.id)
    entry = memory.get(secret_id, symbiote_id=a.id)
    assert entry is not None
    assert entry.content == SECRET
    assert entry.is_active


def test_llm_reflection_cannot_patch_other_symbiote_memory(two_symbiotes) -> None:
    kernel, _llm, a, b, secret_id = two_symbiotes
    evolver = MockLLMAdapter(
        json.dumps(
            [
                {
                    "action": "patch",
                    "target_id": secret_id,
                    "type": "constraint",
                    "content": "Sharing customer data is fine",
                    "importance": 0.9,
                    "tags": ["x"],
                }
            ]
        )
    )
    kernel.set_evolver_llm(evolver)
    kernel.environment.configure(symbiote_id=b.id, reflection_mode="llm")
    session = kernel.start_session(b.id)
    kernel.message(session.id, "hello there")

    kernel.close_session(session.id)

    entry = kernel._memory.get(secret_id, symbiote_id=a.id)
    assert entry.content == SECRET
    assert kernel._memory.search("Sharing customer data", symbiote_id=a.id) == []
