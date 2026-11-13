"""Regression: keyword reflection learns from the user in English and
Portuguese, ignores the assistant, and closing a reopened session does not
duplicate memories or scores (report item M-3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import ScriptedLLM, make_kernel


def _memories(kernel, symbiote_id: str) -> list[str]:
    rows = kernel._storage.fetch_all(
        "SELECT content FROM memory_entries WHERE symbiote_id = ? AND is_active = 1",
        (symbiote_id,),
    )
    return [r["content"] for r in rows]


@pytest.mark.parametrize(
    "message",
    [
        "Eu prefiro respostas curtas e diretas.",
        "Lembre-se: eu sempre uso Python 3.12.",
        "Nunca faça deploy na sexta-feira.",
        "Decidi que o deploy será toda terça-feira.",
        "We decided to deploy every Friday.",
        "I prefer short and direct answers.",
    ],
)
def test_user_statements_become_memories(tmp_path: Path, message: str) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("Understood."))
    sym = kernel.create_symbiote(name="a", role="assistant")
    session = kernel.start_session(sym.id)

    kernel.message(session.id, message)
    kernel.close_session(session.id)

    assert _memories(kernel, sym.id) == [message]
    kernel.shutdown()


def test_assistant_messages_are_not_learned(tmp_path: Path) -> None:
    reply = "Sure. As a rule I never store secrets in plain text, and I always double-check."
    kernel = make_kernel(tmp_path, ScriptedLLM(reply))
    sym = kernel.create_symbiote(name="a", role="assistant")
    session = kernel.start_session(sym.id)

    kernel.message(session.id, "hello there, how are you today?")
    kernel.close_session(session.id)

    assert _memories(kernel, sym.id) == []
    kernel.shutdown()


def test_words_must_match_whole(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sym = kernel.create_symbiote(name="a", role="assistant")
    session = kernel.start_session(sym.id)

    kernel.message(session.id, "The ruler and the neverland map are on my desk.")
    kernel.close_session(session.id)

    assert _memories(kernel, sym.id) == []
    kernel.shutdown()


def test_reopened_session_closed_four_times(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sym = kernel.create_symbiote(name="a", role="assistant")
    session = kernel.get_or_create_session(sym.id, external_key="user-1:chat")
    kernel.message(session.id, "I prefer dark mode in every editor.")
    kernel.close_session(session.id)

    for _ in range(3):
        session = kernel.get_or_create_session(sym.id, external_key="user-1:chat")
        kernel.message(session.id, "ok thanks")
        kernel.close_session(session.id)

    assert _memories(kernel, sym.id) == ["I prefer dark mode in every editor."]
    scores = kernel._storage.fetch_all(
        "SELECT id FROM session_scores WHERE session_id = ?", (session.id,)
    )
    assert len(scores) == 1
    kernel.shutdown()


def test_get_session_does_not_reopen(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sym = kernel.create_symbiote(name="a", role="assistant")
    session = kernel.start_session(sym.id)
    kernel.close_session(session.id)

    assert kernel.get_session(session.id).status == "closed"
    row = kernel._storage.fetch_one("SELECT status FROM sessions WHERE id = ?", (session.id,))
    assert row["status"] == "closed"
    kernel.shutdown()


def test_api_get_session_does_not_reopen(api_client) -> None:
    api = api_client()
    h = api.auth(api.new_key("tenant-a"))
    sym = api.client.post("/symbiotes", headers=h, json={"name": "a", "role": "r"}).json()
    sess = api.client.post("/sessions", headers=h, json={"symbiote_id": sym["id"]}).json()
    api.client.post(f"/sessions/{sess['id']}/close", headers=h)

    assert api.client.get(f"/sessions/{sess['id']}", headers=h).json()["status"] == "closed"
