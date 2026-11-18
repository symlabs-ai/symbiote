"""Regression: optional retention of messages and traces of long-closed
sessions; off by default, never touches active sessions (report items P-2,
D-12).

B-100 / B-101: the windows a host needs for "nothing of a conversation is
kept past N days" — the session summary (the default one is message text),
the audit rows, the sessions themselves, and a session that is never closed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError as ConfigError
from typer.testing import CliRunner

from symbiote.cli.main import app
from symbiote.config.models import KernelConfig
from symbiote.core.exceptions import ValidationError
from tests.support import ScriptedLLM, make_kernel


def _counts(**deleted: int) -> dict[str, int]:
    """What ``prune()`` returns: one entry per table it covers."""
    return {
        "messages": 0,
        "execution_traces": 0,
        "turn_contexts": 0,
        "memory_entries": 0,
        "llm_usage": 0,
        "audit_log": 0,
        "reflection_audit": 0,
        "skill_review_audit": 0,
        "pending_approvals": 0,
        "decisions": 0,
        "process_instances": 0,
        "artifacts": 0,
        "session_locks": 0,
        "session_scores": 0,
        "summaries": 0,
        "sessions": 0,
        **deleted,
    }


def _count(kernel, table: str, session_id: str) -> int:
    return kernel._storage.fetch_one(
        f"SELECT COUNT(*) c FROM {table} WHERE session_id = ?", (session_id,)  # noqa: S608
    )["c"]


def _session(kernel, sid: str, *, closed_days_ago: float | None) -> str:
    session = kernel.start_session(sid)
    kernel._sessions.add_message(session.id, "user", "hi")
    kernel._storage.execute(
        "INSERT INTO execution_traces (id, session_id, symbiote_id, total_iterations, "
        "total_tool_calls, total_elapsed_ms, stop_reason, steps_json, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (str(uuid4()), session.id, sid, 1, 0, 1, "end_turn", "[]", datetime.now(tz=UTC).isoformat()),
    )
    if closed_days_ago is not None:
        ended = (datetime.now(tz=UTC) - timedelta(days=closed_days_ago)).isoformat()
        kernel._storage.execute(
            "UPDATE sessions SET status = 'closed', ended_at = ? WHERE id = ?", (ended, session.id)
        )
    return session.id


def _seed(kernel):
    sid = kernel.create_symbiote(name="a", role="r").id
    return {
        "old": _session(kernel, sid, closed_days_ago=40),
        "recent": _session(kernel, sid, closed_days_ago=2),
        "active": _session(kernel, sid, closed_days_ago=None),
    }


def test_retention_is_off_by_default(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    s = _seed(kernel)

    assert kernel.prune() == _counts()
    assert _count(kernel, "messages", s["old"]) == 1
    kernel.shutdown()


def test_retention_removes_only_old_closed_sessions(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"),
                         message_retention_days=30, trace_retention_days=30)
    s = _seed(kernel)
    # An active session with an ancient ended_at (reopened) is still active.
    kernel._storage.execute(
        "UPDATE sessions SET ended_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (s["active"],)
    )

    dry = kernel.prune(dry_run=True)
    assert dry == _counts(messages=1, execution_traces=1)
    assert _count(kernel, "messages", s["old"]) == 1

    assert kernel.prune() == _counts(messages=1, execution_traces=1)
    assert _count(kernel, "messages", s["old"]) == 0
    assert _count(kernel, "execution_traces", s["old"]) == 0
    for keep in ("recent", "active"):
        assert _count(kernel, "messages", s[keep]) == 1
        assert _count(kernel, "execution_traces", s[keep]) == 1
    kernel.shutdown()


def test_cli_prune_with_explicit_windows(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"), db_path=db)
    s = _seed(kernel)
    kernel.shutdown()

    runner = CliRunner()
    dry = runner.invoke(app, ["--db-path", str(db), "prune", "--messages-days", "30", "--dry-run"])
    live = runner.invoke(app, ["--db-path", str(db), "prune", "--messages-days", "30",
                               "--traces-days", "30"])

    assert dry.exit_code == 0, dry.output
    assert live.exit_code == 0, live.output
    reopened = make_kernel(tmp_path, ScriptedLLM("ok"), db_path=db)
    assert _count(reopened, "messages", s["old"]) == 0
    assert _count(reopened, "messages", s["recent"]) == 1
    reopened.shutdown()


# ── B-100: the summary is message text ───────────────────────────────────


def _ago(days: float) -> str:
    return (datetime.now(tz=UTC) - timedelta(days=days)).isoformat()


def _close(kernel, session_id: str, *, days_ago: float) -> None:
    """Close the session the way a host does, then make it that old."""
    kernel.close_session(session_id)
    kernel._storage.execute(
        "UPDATE sessions SET ended_at = ? WHERE id = ?", (_ago(days_ago), session_id)
    )


def _summary(kernel, session_id: str) -> str | None:
    return kernel._storage.fetch_one(
        "SELECT summary FROM sessions WHERE id = ?", (session_id,)
    )["summary"]


def test_the_message_window_alone_leaves_the_text_in_the_summary(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("noted"))
    sid = kernel.create_symbiote(name="a", role="r").id
    session = kernel.start_session(sid)
    kernel.message(session.id, "my card ends in 4242")
    _close(kernel, session.id, days_ago=40)

    assert kernel.prune(messages_days=30)["messages"] == 2
    assert _count(kernel, "messages", session.id) == 0
    assert "4242" in _summary(kernel, session.id)  # kept: summaries have their own window

    assert kernel.prune(summaries_days=30, dry_run=True)["summaries"] == 1
    assert "4242" in _summary(kernel, session.id)
    assert kernel.prune(summaries_days=30)["summaries"] == 1
    assert _summary(kernel, session.id) is None
    assert kernel.prune(summaries_days=30)["summaries"] == 0  # nothing left to clear
    kernel.shutdown()


def test_the_summary_window_reaches_only_sessions_closed_long_ago(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("noted"), summary_retention_days=30)
    sid = kernel.create_symbiote(name="a", role="r").id
    old, recent, active = (kernel.start_session(sid).id for _ in range(3))
    for session_id in (old, recent, active):
        kernel.message(session_id, "hello")
    _close(kernel, old, days_ago=40)
    _close(kernel, recent, days_ago=2)

    assert kernel.prune() == _counts(summaries=1)

    assert _summary(kernel, old) is None
    assert "hello" in _summary(kernel, recent)
    assert _count(kernel, "messages", old) == 2  # the summary window deletes no message
    kernel.shutdown()


# ── B-101: a session that is never closed ────────────────────────────────


def _contents(kernel, session_id: str) -> list[str]:
    rows = kernel._storage.fetch_all(
        "SELECT content FROM messages WHERE session_id = ? ORDER BY created_at", (session_id,)
    )
    return [r["content"] for r in rows]


def _long_conversation(kernel) -> str:
    """One session, never closed: a turn 40 days ago and a turn today."""
    sid = kernel.create_symbiote(name="a", role="r").id
    session = kernel.start_session(sid)
    kernel.message(session.id, "old question")
    for table in ("messages", "turn_contexts", "execution_traces"):
        kernel._storage.execute(
            f"UPDATE {table} SET created_at = ? WHERE session_id = ?",  # noqa: S608
            (_ago(40), session.id),
        )
    kernel.message(session.id, "new question")
    return session.id


def test_by_age_the_old_turns_leave_a_session_that_stays_open(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"),
                         message_retention_days=30, trace_retention_days=30)
    session_id = _long_conversation(kernel)
    assert _count(kernel, "turn_contexts", session_id) == 2

    assert kernel.prune() == _counts()  # by closed session: nothing ever leaves it
    dry = kernel.prune(basis="age", dry_run=True)
    assert (dry["messages"], dry["turn_contexts"]) == (2, 1)
    assert len(_contents(kernel, session_id)) == 4

    assert kernel.prune(basis="age") == dry
    assert _contents(kernel, session_id) == ["new question", "ok"]
    assert _count(kernel, "turn_contexts", session_id) == 1

    kernel.message(session_id, "and another")  # the conversation goes on
    assert len(_contents(kernel, session_id)) == 4
    kernel.shutdown()


def test_by_age_is_a_setting_and_one_call_can_say_otherwise(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"),
                         message_retention_days=30, retention_basis="age")
    session_id = _long_conversation(kernel)

    assert kernel.prune(basis="closed") == _counts()
    assert kernel.prune()["messages"] == 2
    assert _contents(kernel, session_id) == ["new question", "ok"]
    kernel.shutdown()


def test_by_age_also_reaches_old_messages_of_a_session_closed_yesterday(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    session_id = _long_conversation(kernel)
    _close(kernel, session_id, days_ago=1)

    assert kernel.prune(messages_days=30)["messages"] == 0
    assert kernel.prune(messages_days=30, basis="age")["messages"] == 2
    assert _contents(kernel, session_id) == ["new question", "ok"]
    kernel.shutdown()


def test_an_unknown_basis_is_refused(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    with pytest.raises(ValidationError, match="basis"):
        kernel.prune(messages_days=30, basis="idle")
    kernel.shutdown()
    with pytest.raises(ConfigError):
        KernelConfig(retention_basis="idle")


# ── B-101: audit rows ────────────────────────────────────────────────────


def _audit_rows(kernel, sid: str, session_id: str, *, days_ago: float) -> None:
    when = _ago(days_ago)
    kernel._storage.execute(
        "INSERT INTO audit_log (id, symbiote_id, session_id, tool_id, action, params_json, "
        "result, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (str(uuid4()), sid, session_id, "lookup", "execute", '{"q": "4242"}', "success", when),
    )
    kernel._storage.execute(
        "INSERT INTO reflection_audit (id, session_id, symbiote_id, mode, created_at) "
        "VALUES (?,?,?,?,?)",
        (str(uuid4()), session_id, sid, "keyword", when),
    )
    kernel._storage.execute(
        "INSERT INTO skill_review_audit (id, session_id, symbiote_id, trigger, created_at) "
        "VALUES (?,?,?,?,?)",
        (str(uuid4()), session_id, sid, "final", when),
    )


def _approval(kernel, sid: str, session_id: str, status: str, *,
              created_days_ago: float, resolved_days_ago: float | None = None) -> str:
    approval_id = str(uuid4())
    kernel._storage.execute(
        "INSERT INTO pending_approvals (id, symbiote_id, session_id, tool_id, params_json, "
        "status, created_at, resolved_at) VALUES (?,?,?,?,?,?,?,?)",
        (approval_id, sid, session_id, "refund", '{"order": "4242"}', status,
         _ago(created_days_ago),
         None if resolved_days_ago is None else _ago(resolved_days_ago)),
    )
    return approval_id


def _ids(kernel, table: str) -> set[str]:
    return {r["id"] for r in kernel._storage.fetch_all(f"SELECT id FROM {table}")}  # noqa: S608


def test_the_audit_window_goes_by_age_whatever_the_session(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"), audit_retention_days=30)
    sid = kernel.create_symbiote(name="a", role="r").id
    active = kernel.start_session(sid).id  # never closed: the rows still age
    _audit_rows(kernel, sid, active, days_ago=40)
    _audit_rows(kernel, sid, active, days_ago=2)
    _approval(kernel, sid, active, "executed", created_days_ago=50, resolved_days_ago=40)
    waiting = _approval(kernel, sid, active, "pending", created_days_ago=50)
    just_decided = _approval(kernel, sid, active, "denied",
                             created_days_ago=50, resolved_days_ago=1)

    expected = _counts(audit_log=1, reflection_audit=1, skill_review_audit=1,
                       pending_approvals=1)
    assert kernel.prune(dry_run=True) == expected
    assert kernel.prune() == expected

    for table in ("audit_log", "reflection_audit", "skill_review_audit"):
        assert _count(kernel, table, active) == 1
    assert _ids(kernel, "pending_approvals") == {waiting, just_decided}
    assert kernel.prune() == _counts()
    kernel.shutdown()


# ── B-101: the sessions themselves ───────────────────────────────────────


def _full_session(kernel, sid: str, *, closed_days_ago: float | None) -> str:
    """A session with a row in every table that hangs on one."""
    session_id = _session(kernel, sid, closed_days_ago=closed_days_ago)
    _audit_rows(kernel, sid, session_id, days_ago=0)
    _approval(kernel, sid, session_id, "pending", created_days_ago=0)
    kernel._storage.execute(
        "INSERT INTO session_scores (id, session_id, symbiote_id, computed_at) VALUES (?,?,?,?)",
        (str(uuid4()), session_id, sid, _ago(0)),
    )
    kernel._storage.execute(
        "INSERT INTO turn_contexts (id, session_id, symbiote_id, created_at) VALUES (?,?,?,?)",
        (str(uuid4()), session_id, sid, _ago(0)),
    )
    kernel._storage.execute(
        "INSERT INTO decisions (id, session_id, title, created_at) VALUES (?,?,?,?)",
        (str(uuid4()), session_id, "ship it", _ago(0)),
    )
    kernel.capabilities.learn(sid, session_id, "prefers short answers")
    return session_id


_PER_SESSION = (
    "messages", "execution_traces", "turn_contexts", "audit_log", "reflection_audit",
    "skill_review_audit", "pending_approvals", "decisions", "session_scores",
)


def test_the_session_window_erases_a_long_closed_session_whole(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"), session_retention_days=30)
    sid = kernel.create_symbiote(name="a", role="r").id
    old = _full_session(kernel, sid, closed_days_ago=40)
    recent = _full_session(kernel, sid, closed_days_ago=2)
    active = _full_session(kernel, sid, closed_days_ago=None)

    expected = _counts(sessions=1, **dict.fromkeys(_PER_SESSION, 1))
    assert kernel.prune(dry_run=True) == expected
    assert old in _ids(kernel, "sessions")
    assert kernel.prune() == expected

    assert _ids(kernel, "sessions") == {recent, active}
    for table in _PER_SESSION:
        assert _count(kernel, table, old) == 0
        assert _count(kernel, table, recent) == _count(kernel, table, active) == 1
    # Long-term memory outlives the session it came from.
    assert _count(kernel, "memory_entries", old) == 1
    assert kernel.prune() == _counts()
    kernel.shutdown()


def test_windows_that_overlap_count_each_row_once(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sid = kernel.create_symbiote(name="a", role="r").id
    old = _full_session(kernel, sid, closed_days_ago=40)
    windows = {"messages_days": 30, "traces_days": 30, "audit_days": 30,
               "summaries_days": 30, "sessions_days": 30, "basis": "age"}
    kernel._storage.execute("UPDATE sessions SET summary = 'hi' WHERE id = ?", (old,))

    dry = kernel.prune(dry_run=True, **windows)
    assert dry == _counts(sessions=1, summaries=1, **dict.fromkeys(_PER_SESSION, 1))
    assert kernel.prune(**windows) == dry
    assert _ids(kernel, "sessions") == set()
    kernel.shutdown()


def test_a_key_whose_session_was_erased_starts_a_new_one(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sid = kernel.create_symbiote(name="a", role="r").id
    first = kernel.get_or_create_session(sid, external_key="chat-1")
    kernel.message(first.id, "hello")
    _close(kernel, first.id, days_ago=40)

    assert kernel.prune(sessions_days=30)["sessions"] == 1

    again = kernel.get_or_create_session(sid, external_key="chat-1")
    assert again.id != first.id
    kernel.message(again.id, "hello again")
    assert _contents(kernel, again.id) == ["hello again", "ok"]
    kernel.shutdown()


def test_cli_prune_with_the_new_windows(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"), db_path=db)
    sid = kernel.create_symbiote(name="a", role="r").id
    old = _full_session(kernel, sid, closed_days_ago=40)
    kernel._storage.execute("UPDATE sessions SET summary = 'hi' WHERE id = ?", (old,))
    open_session = _long_conversation(kernel)
    kernel.shutdown()

    runner = CliRunner()
    base = ["--db-path", str(db), "prune"]
    dry = runner.invoke(app, [*base, "--sessions-days", "30", "--dry-run"])
    cleared = runner.invoke(app, [*base, "--summaries-days", "30"])
    live = runner.invoke(app, [*base, "--messages-days", "30", "--by-age", "--audit-days", "30",
                               "--usage-days", "30", "--sessions-days", "30"])

    for result in (dry, cleared, live):
        assert result.exit_code == 0, result.output
    assert "Would delete 1 row(s) from sessions" in dry.output
    assert "Cleared 1 session summary(ies)" in cleared.output
    reopened = make_kernel(tmp_path, ScriptedLLM("ok"), db_path=db)
    assert old not in _ids(reopened, "sessions")
    assert _contents(reopened, open_session) == ["new question", "ok"]
    reopened.shutdown()
