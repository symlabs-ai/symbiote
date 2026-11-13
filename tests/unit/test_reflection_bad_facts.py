"""Regression: malformed LLM reflection output never breaks close_session
(report item M-4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from symbiote.adapters.llm.base import MockLLMAdapter
from tests.support import ScriptedLLM, make_kernel

GOOD = {"action": "create", "type": "preference", "content": "User prefers dark mode", "importance": 0.6, "tags": ["ui"]}


@pytest.mark.parametrize("bad_importance", ["high", None, -0.2, 1.5, [1]])
def test_bad_fact_is_skipped_and_session_closes(tmp_path: Path, bad_importance) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    bad = {"action": "create", "type": "constraint", "content": "Deploys happen on Tuesdays",
           "importance": bad_importance, "tags": ["deploy"]}
    kernel.set_evolver_llm(MockLLMAdapter(json.dumps([GOOD, bad])))
    sym = kernel.create_symbiote(name="a", role="assistant")
    kernel.environment.configure(symbiote_id=sym.id, reflection_mode="llm")
    session = kernel.start_session(sym.id)
    kernel.message(session.id, "I like dark mode; we deploy on Tuesdays")

    closed = kernel.close_session(session.id)

    assert closed.status == "closed"
    contents = [
        r["content"] for r in kernel._storage.fetch_all(
            "SELECT content, importance FROM memory_entries WHERE symbiote_id = ?", (sym.id,)
        )
    ]
    assert "User prefers dark mode" in contents
    for row in kernel._storage.fetch_all(
        "SELECT importance FROM memory_entries WHERE symbiote_id = ?", (sym.id,)
    ):
        assert 0.0 <= row["importance"] <= 1.0
    audit = kernel._storage.fetch_one(
        "SELECT llm_error FROM reflection_audit WHERE session_id = ?", (session.id,)
    )
    assert audit is not None
    kernel.shutdown()


def test_failing_close_step_still_closes(tmp_path: Path, monkeypatch) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sym = kernel.create_symbiote(name="a", role="assistant")
    session = kernel.start_session(sym.id)
    kernel.message(session.id, "hello there")

    def boom(*args, **kwargs):
        raise RuntimeError("reflection exploded")

    monkeypatch.setattr(kernel, "_reflection_for", lambda sid: type("R", (), {"reflect_session": boom})())

    closed = kernel.close_session(session.id)

    assert closed.status == "closed"
    assert kernel._storage.fetch_one(
        "SELECT id FROM session_scores WHERE session_id = ?", (session.id,)
    ) is not None
    kernel.shutdown()
