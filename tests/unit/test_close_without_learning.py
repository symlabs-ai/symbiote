"""Closing a session without learning from it (B-106).

``close_session(session_id, learn=False)`` only closes: no score, summary,
reflection, failure memory or skill review. The session is marked and nothing
learned for the symbiote as a whole reads it afterwards.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from symbiote.adapters.storage.sqlite import _MIGRATIONS, SCHEMA_VERSION, SQLiteAdapter
from symbiote.core.background_review import BackgroundReviewEngine
from symbiote.core.models import MemoryEntry
from symbiote.dream.models import BudgetTracker, DreamContext
from symbiote.dream.phases import EvaluatePhase, MinePhase
from symbiote.harness.evolver import EVOLVABLE_COMPONENTS, HarnessEvolver
from symbiote.harness.tuner import ParameterTuner
from symbiote.harness.versions import HarnessVersionRepository
from tests.support import ScriptedLLM, make_kernel

# Lines the keyword reflection turns into memories.
FACTS = (
    "I prefer short and direct answers.",
    "Remember: I always use Python 3.12 and never use tabs.",
)


@pytest.fixture()
def bot(tmp_path: Path):
    llm = ScriptedLLM("Understood.")
    kernel = make_kernel(tmp_path, llm)
    sym = kernel.create_symbiote(name="Bot", role="assistant")
    yield kernel, llm, sym
    kernel.shutdown()


def _talk(kernel, symbiote_id: str, **kw) -> str:
    session = kernel.start_session(symbiote_id, **kw)
    for text in FACTS:
        kernel.message(session.id, text)
    return session.id


def _failed_trace(kernel, session_id: str, symbiote_id: str) -> None:
    """A turn whose tool loop ran out of iterations."""
    kernel._storage.execute(
        "INSERT INTO execution_traces "
        "(id, session_id, symbiote_id, total_iterations, total_tool_calls, "
        "total_elapsed_ms, stop_reason, steps_json, tool_mode, created_at) "
        "VALUES (?, ?, ?, 10, 10, 5, 'max_iterations', ?, 'brief', ?)",
        (
            str(uuid4()),
            session_id,
            symbiote_id,
            '[{"iteration": 1, "tool_id": "search", "params": {}, "success": false, "error": "boom"}]',
            datetime.now(tz=UTC).isoformat(),
        ),
    )


def _memories(kernel, session_id: str) -> list[dict]:
    return kernel._storage.fetch_all(
        "SELECT * FROM memory_entries WHERE session_id = ?", (session_id,)
    )


def _scores(kernel, session_id: str) -> list[dict]:
    return kernel.inspector.list_scores(session_id=session_id)


# ── what the close does not do ──────────────────────────────────────────


def test_a_normal_close_still_learns(bot) -> None:
    kernel, _llm, sym = bot
    sid = _talk(kernel, sym.id)
    _failed_trace(kernel, sid, sym.id)

    closed = kernel.close_session(sid)

    assert closed.learn is True
    assert closed.summary
    assert _scores(kernel, sid)
    assert {m["type"] for m in _memories(kernel, sid)} >= {"procedural"}
    assert len(_memories(kernel, sid)) > 1


def test_closing_without_learning_only_closes(bot) -> None:
    kernel, llm, sym = bot
    sid = _talk(kernel, sym.id)
    _failed_trace(kernel, sid, sym.id)
    calls = len(llm.calls)

    closed = kernel.close_session(sid, learn=False)

    assert (closed.status, closed.learn, closed.summary) == ("closed", False, None)
    assert closed.ended_at is not None
    assert _scores(kernel, sid) == []
    assert _memories(kernel, sid) == []
    assert len(llm.calls) == calls
    stored = kernel.get_session(sid)
    assert (stored.status, stored.learn, stored.summary) == ("closed", False, None)


def test_messages_and_traces_stay(bot) -> None:
    kernel, _llm, sym = bot
    sid = _talk(kernel, sym.id)
    _failed_trace(kernel, sid, sym.id)

    kernel.close_session(sid, learn=False)

    assert len(kernel.inspector.list_messages(sid)) == 2 * len(FACTS)
    assert len(kernel.inspector.list_traces(session_id=sid)) == 1


def test_what_was_learned_during_the_conversation_is_not_undone(bot) -> None:
    kernel, _llm, sym = bot
    sid = _talk(kernel, sym.id)
    kernel._memory.store(
        MemoryEntry(
            symbiote_id=sym.id,
            session_id=sid,
            type="factual",
            scope="global",
            source="user",
            content="The office opens at nine",
        )
    )

    kernel.close_session(sid, learn=False)

    assert [m["content"] for m in _memories(kernel, sid)] == ["The office opens at nine"]


def test_no_llm_summary_is_written(bot) -> None:
    kernel, llm, sym = bot
    kernel.environment.configure(symbiote_id=sym.id, session_summary_mode="llm")
    sid = _talk(kernel, sym.id)
    calls = len(llm.calls)

    closed = kernel.close_session(sid, learn=False)

    assert closed.summary is None
    assert len(llm.calls) == calls


def test_a_pending_handoff_does_not_become_a_memory(bot) -> None:
    kernel, _llm, sym = bot
    sid = _talk(kernel, sym.id)
    kernel._handoffs[sid] = {"pending_blocks": ["write the report"]}

    kernel.close_session(sid, learn=False)

    assert sid not in kernel._handoffs
    assert _memories(kernel, sid) == []


def test_an_unknown_session_is_still_an_error(bot) -> None:
    from symbiote.core.exceptions import EntityNotFoundError

    kernel, _llm, _sym = bot

    with pytest.raises(EntityNotFoundError):
        kernel.close_session("nope", learn=False)


# ── the mark keeps the session out of what is learned ───────────────────


def test_the_inspector_returns_the_mark(bot) -> None:
    kernel, _llm, sym = bot
    kept = _talk(kernel, sym.id)
    learned = _talk(kernel, sym.id)
    still_open = _talk(kernel, sym.id)
    kernel.close_session(kept, learn=False)
    kernel.close_session(learned)

    marks = {s["id"]: s["learn"] for s in kernel.inspector.list_sessions(sym.id)}

    assert marks == {kept: False, learned: True, still_open: True}


def _readers(kernel):
    return (
        ParameterTuner(kernel._storage),
        HarnessEvolver(kernel._storage, HarnessVersionRepository(kernel._storage)),
    )


def test_a_session_already_scored_leaves_the_tuner_and_the_evolver(bot) -> None:
    """Closed with learning, reopened, then closed without: the score and the
    traces it left stop being read."""
    kernel, _llm, sym = bot
    other = _talk(kernel, sym.id)
    kernel.close_session(other)
    sid = _talk(kernel, sym.id)
    _failed_trace(kernel, sid, sym.id)
    kernel.close_session(sid)
    kernel.report_feedback(sid, 0.0)
    tuner, evolver = _readers(kernel)
    assert {s["session_id"] for s in tuner._get_recent_scores(sym.id, 7)} == {other, sid}
    assert {s["session_id"] for s in evolver._get_failed_sessions(sym.id, 7)} == {sid}

    kernel._sessions.resume(sid)
    kernel.close_session(sid, learn=False)

    assert {s["session_id"] for s in tuner._get_recent_scores(sym.id, 7)} == {other}
    assert [t["session_id"] for t in tuner._get_recent_traces(sym.id, 7)] == []
    assert evolver._get_failed_sessions(sym.id, 7) == []
    assert {s["session_id"] for s in evolver._get_successful_sessions(sym.id, 7)} == {other}
    assert kernel._session_feeds_learning(sid, sym.id) is False
    # The score itself is history, not undone.
    assert len(_scores(kernel, sid)) == 1


def test_the_harness_versions_get_no_score(bot) -> None:
    kernel, _llm, sym = bot
    versions = kernel._harness_versions
    for component in EVOLVABLE_COMPONENTS:
        versions.create_version(sym.id, component, "text")

    def counted() -> int:
        return sum(
            int(r["session_count"])
            for r in kernel._storage.fetch_all(
                "SELECT session_count FROM harness_versions WHERE symbiote_id = ?",
                (sym.id,),
            )
        )

    kernel.close_session(_talk(kernel, sym.id), learn=False)
    assert counted() == 0

    kernel.close_session(_talk(kernel, sym.id))
    assert counted() == len(EVOLVABLE_COMPONENTS)


def test_the_dream_does_not_count_it(bot) -> None:
    kernel, _llm, sym = bot
    engine = kernel._build_dream_engine(None, sym.id, min_sessions=1)

    kernel.close_session(_talk(kernel, sym.id), learn=False)
    assert engine.should_dream(sym.id, "light") is False

    kernel.close_session(_talk(kernel, sym.id))
    assert engine.should_dream(sym.id, "light") is True


def test_the_dream_phases_do_not_read_it(bot) -> None:
    kernel, _llm, sym = bot
    sid = _talk(kernel, sym.id)
    _failed_trace(kernel, sid, sym.id)
    kernel.close_session(sid)
    kernel.report_feedback(sid, 0.0)
    dreamer = ScriptedLLM("Check the tool's preconditions before calling it.")

    def run(phase):
        return phase.run(
            DreamContext(
                symbiote_id=sym.id,
                storage=kernel._storage,
                memory=kernel._memory,
                llm=dreamer,
                budget=BudgetTracker(10),
                dry_run=True,
            )
        )

    assert run(MinePhase()).llm_calls_used == 1
    assert run(EvaluatePhase()).llm_calls_used == 1

    kernel._sessions.resume(sid)
    kernel.close_session(sid, learn=False)
    seen = len(dreamer.calls)

    assert run(MinePhase()).llm_calls_used == 0
    assert run(EvaluatePhase()).llm_calls_used == 0
    assert len(dreamer.calls) == seen


def test_skill_review_skips_it(bot) -> None:
    kernel, _llm, sym = bot
    reviewer = ScriptedLLM("[]")
    engine = BackgroundReviewEngine(
        llm=reviewer,
        messages=kernel._message_repo,
        store=kernel._skills_store,
        loader=kernel._skills_loader,
        storage=kernel._storage,
        environment=kernel._environment,
    )
    sid = _talk(kernel, sym.id)
    kernel.close_session(sid, learn=False)

    skipped = engine.run_sync(sid, sym.id)

    assert skipped.get("excluded") is True
    assert reviewer.calls == []


# ── reopening ───────────────────────────────────────────────────────────


def test_the_mark_survives_a_reopen(bot) -> None:
    kernel, _llm, sym = bot
    sid = _talk(kernel, sym.id, external_key="chat-1")
    kernel.close_session(sid, learn=False)

    reopened = kernel.get_or_create_session(sym.id, "chat-1")

    assert (reopened.id, reopened.status, reopened.learn) == (sid, "active", False)
    assert kernel._session_feeds_learning(sid, sym.id) is False


def test_closing_it_again_with_learning_lifts_the_mark(bot) -> None:
    kernel, _llm, sym = bot
    versions = kernel._harness_versions
    for component in EVOLVABLE_COMPONENTS:
        versions.create_version(sym.id, component, "text")
    sid = _talk(kernel, sym.id)
    kernel.close_session(sid, learn=False)
    kernel._sessions.resume(sid)

    closed = kernel.close_session(sid)

    assert closed.learn is True
    assert kernel.get_session(sid).learn is True
    assert len(_scores(kernel, sid)) == 1
    assert _memories(kernel, sid)
    tuner, _evolver = _readers(kernel)
    assert [s["session_id"] for s in tuner._get_recent_scores(sym.id, 7)] == [sid]
    counts = kernel._storage.fetch_all(
        "SELECT session_count FROM harness_versions WHERE symbiote_id = ?", (sym.id,)
    )
    assert [int(r["session_count"]) for r in counts] == [1] * len(EVOLVABLE_COMPONENTS)


# ── databases created before the column ──────────────────────────────────


@pytest.mark.sqlite_only
def test_a_database_without_the_column_gets_it(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"), db_path=db)
    sym = kernel.create_symbiote(name="a", role="assistant")
    sid = _talk(kernel, sym.id)
    kernel.close_session(sid)
    kernel.shutdown()

    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE sessions DROP COLUMN learn")
    conn.execute(f"PRAGMA user_version = {_MIGRATIONS.index('_migrate_session_learn')}")
    conn.commit()
    conn.close()

    adapter = SQLiteAdapter(db)
    adapter.init_schema()
    row = adapter.fetch_one("SELECT status, learn FROM sessions WHERE id = ?", (sid,))
    assert row == {"status": "closed", "learn": 1}
    assert adapter._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    adapter.close()


# ── over HTTP, through the SDK and the CLI ───────────────────────────────


def test_the_close_endpoint_takes_learn(api_client) -> None:
    api = api_client(llm=ScriptedLLM("Understood."))
    h = api.auth(api.new_key("tenant-a"))
    sym = api.client.post("/symbiotes", headers=h, json={"name": "a", "role": "r"}).json()

    def session() -> str:
        sid = api.client.post("/sessions", headers=h, json={"symbiote_id": sym["id"]}).json()["id"]
        for text in FACTS:
            api.client.post(f"/sessions/{sid}/chat", headers=h, json={"content": text})
        return sid

    kept, learned = session(), session()

    quiet = api.client.post(f"/sessions/{kept}/close?learn=false", headers=h)
    normal = api.client.post(f"/sessions/{learned}/close", headers=h)

    assert (quiet.status_code, normal.status_code) == (200, 200)
    assert (quiet.json()["status"], quiet.json()["learn"], quiet.json()["summary"]) == (
        "closed",
        False,
        None,
    )
    assert normal.json()["learn"] is True
    assert api.client.get(f"/sessions/{kept}", headers=h).json()["learn"] is False
    assert _scores(api.kernel, kept) == []
    assert _memories(api.kernel, kept) == []
    assert _scores(api.kernel, learned)


def test_the_sdk_sends_learn(monkeypatch) -> None:
    from symbiote.sdk import SymbioteClient

    sent: list[tuple[str, dict]] = []
    client = SymbioteClient(base_url="http://test", api_key="k")
    monkeypatch.setattr(client, "_post", lambda path, body: sent.append((path, body)) or {})

    client.close_session("s1")
    client.close_session("s1", learn=False)

    assert sent == [
        ("/sessions/s1/close", {}),
        ("/sessions/s1/close?learn=false", {}),
    ]
