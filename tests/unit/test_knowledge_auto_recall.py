"""Knowledge kept for search only, entry by entry (B-89).

``context_mode`` decides for the whole symbiote whether knowledge is packed
into the prompt or searched on demand. ``auto_recall=False`` on one entry
keeps that entry out of the packed knowledge while ``search_knowledge`` still
finds it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from symbiote.core.exceptions import EntityNotFoundError
from tests.support import ScriptedLLM, make_kernel, tool_call_block

PACKED = "Acme refund policy: refunds are paid within five days."
SEARCH_ONLY = "Acme refund exceptions: opened software is not refunded."


@pytest.fixture()
def shop(tmp_path: Path):
    llm = ScriptedLLM("ok")
    kernel = make_kernel(tmp_path, llm)
    sym = kernel.create_symbiote(name="Shop", role="assistant")
    kernel.knowledge.register_source(sym.id, "refund policy", content=PACKED)
    hidden = kernel.knowledge.register_source(
        sym.id, "refund exceptions", content=SEARCH_ONLY, auto_recall=False
    )
    yield kernel, llm, sym, hidden
    kernel.shutdown()


def test_search_only_entry_is_not_packed_into_the_prompt(shop) -> None:
    kernel, llm, sym, _hidden = shop

    kernel.message(kernel.start_session(sym.id).id, "what is the Acme refund policy?")

    prompt = llm.prompt_text(0)
    assert "paid within five days" in prompt
    assert "opened software" not in prompt


def test_search_knowledge_still_finds_it(shop) -> None:
    kernel, llm, sym, hidden = shop
    kernel.environment.configure(symbiote_id=sym.id, tools=["search_knowledge"])
    llm._fn = lambda i, _m: (
        tool_call_block("search_knowledge", query="Acme refund exceptions") if i == 0 else "done"
    )
    session = kernel.start_session(sym.id)

    outcome = kernel.turn(session.id, "can I refund opened software?")

    (result,) = outcome.tool_results
    assert result.success
    assert any("opened software" in item["content"] for item in result.output)
    record = kernel.inspector.list_turn_contexts(session_id=session.id)[0]
    assert {"id": hidden.id, "source": "on_demand"} in record["knowledge"]
    assert hidden.id not in [k["id"] for k in record["knowledge"] if k["source"] == "packed"]


def test_entry_can_be_switched_both_ways(shop) -> None:
    kernel, llm, sym, hidden = shop
    session = kernel.start_session(sym.id)

    kernel.knowledge.set_auto_recall(hidden.id, True)
    kernel.message(session.id, "what are the Acme refund exceptions?")
    assert "opened software" in llm.prompt_text()

    kernel.knowledge.set_auto_recall(hidden.id, False)
    kernel.message(session.id, "what are the Acme refund exceptions?")
    assert "opened software" not in llm.calls[-1]["messages"][0]["content"]
    assert kernel.knowledge.get(hidden.id).auto_recall is False

    with pytest.raises(EntityNotFoundError):
        kernel.knowledge.set_auto_recall("no-such-id", False)


def test_explicit_queries_see_every_entry(shop) -> None:
    kernel, _llm, sym, hidden = shop

    found = kernel.knowledge.query(sym.id, "Acme refund")
    packed = kernel.knowledge.query(sym.id, "Acme refund", auto_recall_only=True)

    assert hidden.id in {e.id for e in found}
    assert hidden.id not in {e.id for e in packed}
    assert len(packed) == 1


def test_term_ranking_also_leaves_search_only_entries_out(shop) -> None:
    kernel, _llm, sym, hidden = shop

    # No literal substring match: found by shared terms.
    packed = kernel.knowledge.query(
        sym.id, "are exceptions made for refunding software?", auto_recall_only=True
    )

    assert hidden.id not in {e.id for e in packed}


@pytest.mark.sqlite_only
def test_entries_from_before_the_column_stay_packed(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"), db_path=db)
    sym = kernel.create_symbiote(name="Old", role="assistant")
    kernel.knowledge.register_source(sym.id, "refund policy", content=PACKED)
    kernel.shutdown()
    conn = sqlite3.connect(db)
    conn.executescript(
        "ALTER TABLE knowledge_entries DROP COLUMN auto_recall;"
        "PRAGMA user_version = 12;"  # the migration before ``auto_recall``
    )
    conn.close()

    llm = ScriptedLLM("ok")
    kernel = make_kernel(tmp_path, llm, db_path=db)
    try:
        kernel.message(kernel.start_session(sym.id).id, "what is the Acme refund policy?")
        assert "paid within five days" in llm.prompt_text(0)
        assert kernel.knowledge.list_by_symbiote(sym.id)[0].auto_recall is True
    finally:
        kernel.shutdown()
