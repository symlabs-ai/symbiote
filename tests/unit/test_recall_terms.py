"""Regression: relevant memories are recalled by terms, not only when the
whole message is a substring of the memory (report item M-1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.core.models import MemoryEntry
from tests.support import ScriptedLLM, make_kernel


def _store(
    kernel, symbiote_id: str, content: str, importance: float = 0.9,
    type: str = "preference", **kw,  # noqa: A002 — the MemoryEntry field
) -> str:
    return kernel._memory.store(
        MemoryEntry(
            symbiote_id=symbiote_id,
            type=type,
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
    # A fact: preferences and constraints enter every turn (test below).
    _store(kernel, sym.id, "Unrelated: the cafeteria closes at 3pm", type="factual")
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


def test_preferences_and_constraints_hold_in_every_turn(tmp_path: Path) -> None:
    """A preference that shares no word with the message still reaches the
    model: "short answers" applies to a question about the week too."""
    llm = ScriptedLLM("ok")
    kernel = make_kernel(tmp_path, llm)
    sym = kernel.create_symbiote(name="a", role="assistant")
    _store(kernel, sym.id, "Prefere respostas em tópicos curtos.", importance=0.7)
    _store(kernel, sym.id, "Nunca marcar compromissos antes das 9h.", importance=0.7,
           type="constraint")
    _store(kernel, sym.id, "O Wi-Fi da casa de praia se chamava PraiaNet.", type="factual")
    for i in range(7):  # more than the limit: the most important ones win
        _store(kernel, sym.id, f"Preferência menor número {i}", importance=0.1)
    session = kernel.start_session(sym.id)

    kernel.message(session.id, "O que tenho para fazer esta semana?")

    prompt = llm.prompt_text(0)
    assert "Prefere respostas em tópicos curtos." in prompt
    assert "Nunca marcar compromissos antes das 9h." in prompt
    assert "PraiaNet" not in prompt  # facts still come by relevance
    assert sum(f"Preferência menor número {i}" in prompt for i in range(7)) == 3  # 5 in all
    kernel.shutdown()


def test_standing_memories_count_as_used_and_can_be_turned_off(tmp_path: Path) -> None:
    llm = ScriptedLLM("ok")
    kernel = make_kernel(tmp_path, llm, db_path=tmp_path / "off.db", standing_memory_limit=0)
    sym = kernel.create_symbiote(name="a", role="assistant")
    _store(kernel, sym.id, "Prefere respostas em tópicos curtos.")
    session = kernel.start_session(sym.id)
    kernel.message(session.id, "O que tenho para fazer esta semana?")
    assert "tópicos curtos" not in llm.prompt_text(0)
    kernel.shutdown()

    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sym = kernel.create_symbiote(name="b", role="assistant")
    mem_id = _store(kernel, sym.id, "Prefere respostas em tópicos curtos.")
    before = kernel._memory.get(mem_id).last_used_at
    session = kernel.start_session(sym.id)
    kernel.message(session.id, "O que tenho para fazer esta semana?")
    assert kernel._memory.get(mem_id).last_used_at > before  # the dream sees it in use
    kernel.shutdown()
