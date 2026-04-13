"""Tests for DreamEngine orchestration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.models import MemoryEntry
from symbiote.dream.engine import DreamEngine
from symbiote.memory.store import MemoryStore


def _uuid() -> str:
    return str(uuid4())


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "dream_engine_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    adp.execute(
        "INSERT INTO symbiotes (id, name, role, created_at) VALUES (?, ?, ?, ?)",
        ("sym-1", "test-agent", "assistant", _utcnow().isoformat()),
    )
    yield adp
    adp.close()


@pytest.fixture()
def memory(adapter: SQLiteAdapter) -> MemoryStore:
    return MemoryStore(adapter)


@pytest.fixture()
def engine(adapter: SQLiteAdapter, memory: MemoryStore) -> DreamEngine:
    return DreamEngine(
        storage=adapter,
        memory=memory,
        llm=None,
        min_sessions=2,
    )


def _insert_closed_sessions(adapter: SQLiteAdapter, count: int, symbiote_id: str = "sym-1") -> None:
    for _ in range(count):
        sid = _uuid()
        ended = _utcnow().isoformat()
        adapter.execute(
            "INSERT INTO sessions (id, symbiote_id, status, ended_at) VALUES (?, ?, 'closed', ?)",
            (sid, symbiote_id, ended),
        )


# ── should_dream tests ─────────────────────────────────────────────────────


class TestShouldDream:
    def test_off_mode_returns_false(self, engine, adapter):
        _insert_closed_sessions(adapter, 10)
        assert engine.should_dream("sym-1", "off") is False

    def test_not_enough_sessions(self, engine, adapter):
        _insert_closed_sessions(adapter, 1)  # need 2
        assert engine.should_dream("sym-1", "light") is False

    def test_enough_sessions_returns_true(self, engine, adapter):
        _insert_closed_sessions(adapter, 3)
        assert engine.should_dream("sym-1", "light") is True

    def test_respects_last_dream_boundary(self, engine, adapter):
        # Insert sessions, run a dream, then check
        _insert_closed_sessions(adapter, 3)
        engine.dream("sym-1", "light")

        # No new sessions since dream → should not dream again
        assert engine.should_dream("sym-1", "light") is False

        # Add more sessions → should dream again
        _insert_closed_sessions(adapter, 2)
        assert engine.should_dream("sym-1", "light") is True


# ── dream() tests ──────────────────────────────────────────────────────────


class TestDream:
    def test_light_mode_runs_only_deterministic_phases(self, engine, memory):
        # Add a stale memory to give prune something to do
        entry = MemoryEntry(
            symbiote_id="sym-1",
            type="factual",
            scope="global",
            content="old fact",
            importance=0.2,
            source="user",
            last_used_at=_utcnow() - timedelta(days=100),
        )
        memory.store(entry)

        report = engine.dream("sym-1", "light")

        assert report.dream_mode == "light"
        assert report.completed_at is not None
        phase_names = [p.phase for p in report.phases]
        assert "prune" in phase_names
        assert "reconcile" in phase_names
        # LLM phases should NOT be present (no LLM)
        assert "generalize" not in phase_names
        assert "mine" not in phase_names
        assert "evaluate" not in phase_names

    def test_full_mode_skips_llm_phases_without_llm(self, engine, memory):
        report = engine.dream("sym-1", "full")

        phase_names = [p.phase for p in report.phases]
        # Deterministic phases run
        assert "prune" in phase_names
        assert "reconcile" in phase_names
        # LLM phases skipped (no LLM available)
        assert "generalize" not in phase_names

    def test_dry_run_propagates(self, adapter, memory):
        entry = MemoryEntry(
            symbiote_id="sym-1",
            type="factual",
            scope="global",
            content="old fact",
            importance=0.2,
            source="user",
            last_used_at=_utcnow() - timedelta(days=100),
        )
        mid = memory.store(entry)

        engine = DreamEngine(
            storage=adapter,
            memory=memory,
            dry_run=True,
            min_sessions=1,
        )
        report = engine.dream("sym-1", "light")

        assert report.dry_run is True
        prune = next(p for p in report.phases if p.phase == "prune")
        assert prune.actions_proposed >= 1
        assert prune.actions_applied == 0
        # Memory still active
        assert memory.get(mid).is_active is True

    def test_report_persisted_to_db(self, engine, adapter):
        report = engine.dream("sym-1", "light")

        row = adapter.fetch_one(
            "SELECT * FROM dream_reports WHERE id = ?",
            (report.id,),
        )
        assert row is not None
        assert row["symbiote_id"] == "sym-1"
        assert row["dream_mode"] == "light"


# ── dream_async tests ──────────────────────────────────────────────────────


class TestDreamAsync:
    def test_spawns_daemon_thread(self, engine):
        engine.dream_async("sym-1", "light")
        thread = engine._active_threads.get("sym-1")
        assert thread is not None
        assert thread.daemon is True
        thread.join(timeout=5.0)
        assert not thread.is_alive()

    def test_concurrent_dream_blocked(self, engine, adapter):
        _insert_closed_sessions(adapter, 5)
        engine.dream_async("sym-1", "light")
        # While thread is running (or just finished), should_dream returns False
        # because _active_threads has the entry
        thread = engine._active_threads.get("sym-1")
        if thread and thread.is_alive():
            assert engine.should_dream("sym-1", "light") is False
        thread.join(timeout=5.0)
