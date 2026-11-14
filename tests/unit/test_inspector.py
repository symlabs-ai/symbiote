"""KernelInspector filters used by the admin Console and the CLI (B-78)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.inspector import KernelInspector


@pytest.fixture()
def storage(tmp_path: Path) -> Iterator[SQLiteAdapter]:
    adp = SQLiteAdapter(db_path=tmp_path / "inspector.db", check_same_thread=False)
    adp.init_schema()
    yield adp
    adp.close()


def _score(adp: SQLiteAdapter, sid: str, session: str, sym: str, at: str) -> None:
    adp.execute(
        "INSERT INTO session_scores (id, session_id, symbiote_id, auto_score, final_score, "
        "computed_at) VALUES (?, ?, ?, 0.5, 0.5, ?)",
        (sid, session, sym, at),
    )


def test_list_scores_by_symbiote_or_session(storage: SQLiteAdapter) -> None:
    _score(storage, "sc1", "s1", "symA", "2026-01-01")
    _score(storage, "sc2", "s2", "symA", "2026-01-02")
    _score(storage, "sc3", "s3", "symB", "2026-01-03")
    ins = KernelInspector(storage)

    by_symbiote = ins.list_scores("symA")
    assert [r["id"] for r in by_symbiote] == ["sc2", "sc1"]
    assert by_symbiote[0]["symbiote_id"] == "symA"
    assert [r["id"] for r in ins.list_scores(session_id="s3")] == ["sc3"]
    assert ins.list_scores("symA", session_id="s3") == []
    assert len(ins.list_scores("symA", limit=1)) == 1
    with pytest.raises(ValueError):
        ins.list_scores()


def test_list_reflections_filters(storage: SQLiteAdapter) -> None:
    rows = [
        ("r1", "s1", "symA", "2026-01-01T00:00:00"),
        ("r2", "s2", "symA", "2026-01-05T00:00:00"),
        ("r3", "s3", "symB", "2026-01-06T00:00:00"),
    ]
    for rid, session, sym, at in rows:
        storage.execute(
            "INSERT INTO reflection_audit (id, session_id, symbiote_id, mode, "
            "keyword_facts_json, llm_facts_json, created_at) VALUES (?, ?, ?, 'hybrid', ?, ?, ?)",
            (rid, session, sym, '["a"]', "[]", at),
        )
    ins = KernelInspector(storage)

    assert [r["id"] for r in ins.list_reflections("symA")] == ["r2", "r1"]  # positional, as before
    assert [r["id"] for r in ins.list_reflections()] == ["r3", "r2", "r1"]
    assert [r["id"] for r in ins.list_reflections(session_id="s1")] == ["r1"]
    assert [r["id"] for r in ins.list_reflections(since="2026-01-05")] == ["r3", "r2"]
    assert [r["id"] for r in ins.list_reflections("symA", since="2026-01-05")] == ["r2"]
    newest = ins.list_reflections(limit=1)
    assert len(newest) == 1
    assert newest[0]["keyword_facts"] == ["a"] and newest[0]["llm_facts"] == []


def test_list_skill_reviews_filters(storage: SQLiteAdapter) -> None:
    rows = [
        ("k1", "s1", "symA", "nudge", "2026-01-01T00:00:00"),
        ("k2", "s1", "symA", "final", "2026-01-05T00:00:00"),
        ("k3", "s2", "symB", "final", "2026-01-06T00:00:00"),
    ]
    for kid, session, sym, trigger, at in rows:
        storage.execute(
            "INSERT INTO skill_review_audit (id, session_id, symbiote_id, trigger, ops_json, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (kid, session, sym, trigger, '[{"op": "create"}]', at),
        )
    ins = KernelInspector(storage)

    assert [r["id"] for r in ins.list_skill_reviews("symA")] == ["k2", "k1"]
    assert [r["id"] for r in ins.list_skill_reviews()] == ["k3", "k2", "k1"]
    assert [r["id"] for r in ins.list_skill_reviews(trigger="final")] == ["k3", "k2"]
    assert [r["id"] for r in ins.list_skill_reviews(session_id="s1", trigger="nudge")] == ["k1"]
    assert [r["id"] for r in ins.list_skill_reviews(since="2026-01-05")] == ["k3", "k2"]
    assert ins.list_skill_reviews(limit=1)[0]["ops"] == [{"op": "create"}]
