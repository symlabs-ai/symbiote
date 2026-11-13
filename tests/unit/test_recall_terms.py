"""Regression: relevant memories are recalled by terms, not only when the
whole message is a substring of the memory (report item M-1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.core.models import MemoryEntry
from tests.support import ScriptedLLM, make_kernel


def _store(kernel, symbiote_id: str, content: str, importance: float = 0.9, **kw) -> str:
    return kernel._memory.store(
        MemoryEntry(
            symbiote_id=symbiote_id,
            type="preference",
            scope="global",
            source="user",
            content=content,
            importance=importance,
            **kw,
        )
    )


@pytest.mark.parametrize(
    ("memory", "question"),
    [
        ("User prefers the vim editor", "Which editor do I prefer?"),
        ("Usuário prefere o editor vim", "Qual é o meu editor preferido?"),
        ("Deploys happen every Friday afternoon", "when do we deploy?"),
        ("A senha do Wi-Fi do escritório está no cofre", "onde fica a senha do wifi?"),
    ],
)
def test_question_recalls_related_memory(tmp_path: Path, memory: str, question: str) -> None:
    llm = ScriptedLLM("ok")
    kernel = make_kernel(tmp_path, llm)
    sym = kernel.create_symbiote(name="a", role="assistant")
    _store(kernel, sym.id, memory)
    _store(kernel, sym.id, "Unrelated: the cafeteria closes at 3pm")
    session = kernel.start_session(sym.id)

    kernel.message(session.id, question)

    prompt = llm.prompt_text(0)
    assert memory in prompt
    assert "cafeteria" not in prompt
    kernel.shutdown()


def test_get_relevant_ranks_by_overlap_and_keeps_session_first(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sym = kernel.create_symbiote(name="a", role="assistant")
    session = kernel.start_session(sym.id)
    other = kernel.start_session(sym.id)
    _store(kernel, sym.id, "python packaging uses pyproject", importance=0.9, session_id=other.id)
    _store(kernel, sym.id, "python tests run with pytest", importance=0.2, session_id=session.id)

    found = kernel._memory.get_relevant(
        "how are python tests run?", session.id, symbiote_id=sym.id
    )

    assert [m.content for m in found][0] == "python tests run with pytest"
    assert len(found) == 2
    kernel.shutdown()


def test_no_terms_means_no_recall(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sym = kernel.create_symbiote(name="a", role="assistant")
    _store(kernel, sym.id, "anything at all")

    assert kernel._memory.get_relevant("the a of", symbiote_id=sym.id) == []
    kernel.shutdown()
