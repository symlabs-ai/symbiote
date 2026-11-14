"""Regression: the hot queries use an index instead of scanning the table
(report item P-1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter

# SQLite-specific behaviour (file, pragmas, query plans, legacy migrations).
pytestmark = pytest.mark.sqlite_only


@pytest.fixture()
def adapter(tmp_path: Path):
    a = SQLiteAdapter(db_path=tmp_path / "idx.db")
    a.init_schema()
    yield a
    a.close()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM messages WHERE session_id = 'x' ORDER BY created_at",
        "SELECT COUNT(*) FROM messages WHERE session_id = 'x'",
        "SELECT * FROM memory_entries WHERE symbiote_id = 'x' AND is_active = 1 AND type = 'y'",
        "SELECT * FROM memory_entries WHERE symbiote_id = 'x' AND is_active = 1",
        "SELECT COUNT(*) FROM sessions WHERE symbiote_id = 'x' AND status = 'closed' AND ended_at > 'y'",
        "SELECT * FROM execution_traces WHERE session_id = 'x'",
    ],
)
def test_hot_query_uses_an_index(adapter, sql: str) -> None:
    plan = " | ".join(r["detail"] for r in adapter.fetch_all("EXPLAIN QUERY PLAN " + sql))
    assert "USING" in plan and "INDEX" in plan, plan
    assert not plan.startswith("SCAN "), plan
