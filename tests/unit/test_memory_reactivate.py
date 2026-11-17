"""A deactivated memory can come back (B-83)."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.core.exceptions import EntityNotFoundError
from symbiote.core.models import MemoryEntry
from tests.support import ScriptedLLM, make_kernel

FACT = "Acme office opens at nine"


@pytest.fixture()
def shop(tmp_path: Path):
    llm = ScriptedLLM("ok")
    kernel = make_kernel(tmp_path, llm)
    sym = kernel.create_symbiote(name="Shop", role="assistant")
    yield kernel, llm, sym
    kernel.shutdown()


def _store(kernel, symbiote_id: str, content: str = FACT, **kw) -> str:
    return kernel.memory.store(
        MemoryEntry(
            symbiote_id=symbiote_id, scope="global", source="user", type="factual",
            importance=0.9, content=content, **kw,
        )
    )


def test_reactivated_memory_is_recalled_again(shop) -> None:
    kernel, llm, sym = shop
    memory_id = _store(kernel, sym.id)
    kernel.memory.deactivate(memory_id, symbiote_id=sym.id)
    session = kernel.start_session(sym.id)
    kernel.message(session.id, "when does the Acme office open?")
    assert "opens at nine" not in llm.all_prompts_text()

    kernel.memory.reactivate(memory_id, symbiote_id=sym.id)

    kernel.message(session.id, "when does the Acme office open?")
    assert "opens at nine" in llm.prompt_text()
    assert kernel.memory.get(memory_id, symbiote_id=sym.id).is_active is True
    assert kernel.memory.update(memory_id, importance=0.5, symbiote_id=sym.id) is True


def test_reactivating_an_active_memory_changes_nothing(shop) -> None:
    kernel, _llm, sym = shop
    memory_id = _store(kernel, sym.id)
    before = kernel.inspector.list_memories(sym.id)[0]

    kernel.memory.reactivate(memory_id, symbiote_id=sym.id)

    assert kernel.inspector.list_memories(sym.id)[0] == before


def test_unknown_memory_and_another_symbiotes_memory_are_not_found(shop) -> None:
    kernel, _llm, sym = shop
    other = kernel.create_symbiote(name="Other", role="assistant")
    memory_id = _store(kernel, sym.id)
    kernel.memory.deactivate(memory_id)

    with pytest.raises(EntityNotFoundError):
        kernel.memory.reactivate("no-such-id")
    with pytest.raises(EntityNotFoundError):
        kernel.memory.reactivate(memory_id, symbiote_id=other.id)

    assert kernel.inspector.list_memories(sym.id) == []


def test_reactivate_respects_the_end_users_partition(shop) -> None:
    kernel, _llm, sym = shop
    of_ana = _store(kernel, sym.id, "Acme order of Ana is 4471", subject_key="ana")
    shared = _store(kernel, sym.id)
    kernel.memory.deactivate(of_ana)
    kernel.memory.deactivate(shared)

    with pytest.raises(EntityNotFoundError):
        kernel.memory.reactivate(of_ana, symbiote_id=sym.id, subject_key="bia")
    with pytest.raises(EntityNotFoundError):
        kernel.memory.reactivate(shared, symbiote_id=sym.id, subject_key="ana")
    with pytest.raises(EntityNotFoundError):
        kernel.memory.reactivate(of_ana, symbiote_id=sym.id, subject_key=None)

    kernel.memory.reactivate(of_ana, symbiote_id=sym.id, subject_key="ana")
    active = kernel.inspector.list_memories(sym.id)
    assert [m["id"] for m in active] == [of_ana]
