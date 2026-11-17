"""Regression: optional retention of messages and traces of long-closed
sessions; off by default, never touches active sessions (report items P-2,
D-12)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from typer.testing import CliRunner

from symbiote.cli.main import app
from tests.support import ScriptedLLM, make_kernel


def _counts(**deleted: int) -> dict[str, int]:
    """What ``prune()`` returns: one entry per table it covers."""
    return {
        "messages": 0,
        "execution_traces": 0,
        "turn_contexts": 0,
        "memory_entries": 0,
        "llm_usage": 0,
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
