"""Tests for ProcessEngine and ProcessRunner."""

from __future__ import annotations

import json

import pytest

from symbiote.core.context import AssembledContext
from symbiote.core.exceptions import EntityNotFoundError, ValidationError
from symbiote.core.models import ProcessInstance
from symbiote.process.engine import ProcessDefinition, ProcessEngine, ProcessStep
from symbiote.runners.base import RunResult
from symbiote.runners.process import ProcessRunner

# ── Fake StoragePort ─────────────────────────────────────────────────────────


class FakeStorage:
    """In-memory StoragePort for testing."""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}

    def execute(self, sql: str, params: tuple | None = None) -> None:
        if sql.strip().upper().startswith("INSERT"):
            # Extract id from params (first param)
            if params:
                row_id = params[0]
                self._rows[row_id] = {"params": params, "sql": sql}

        elif sql.strip().upper().startswith("UPDATE") and params and params[-1] in self._rows:
            row_id = params[-1]
            self._rows[row_id]["params"] = params
            self._rows[row_id]["sql"] = sql

    def fetch_one(self, sql: str, params: tuple | None = None) -> dict | None:
        if params and params[0] in self._rows:
            stored = self._rows[params[0]]
            p = stored["params"]
            return {
                "id": p[0],
                "session_id": p[1],
                "process_name": p[2],
                "state": p[3],
                "current_step": p[4],
                "logs_json": p[5],
                "created_at": p[6],
                "updated_at": p[7],
            }
        return None

    def fetch_all(self, sql: str, params: tuple | None = None) -> list[dict]:
        return []

    def close(self) -> None:
        pass


# ── ProcessDefinition model ─────────────────────────────────────────────────


class TestProcessDefinition:
    def test_minimal_definition(self):
        d = ProcessDefinition(name="test")
        assert d.name == "test"
        assert d.steps == []
        assert d.reflection_policy == "on_completion"

    def test_definition_with_steps(self):
        d = ProcessDefinition(
            name="flow",
            steps=[
                ProcessStep(name="step1", description="first"),
                ProcessStep(name="step2"),
            ],
        )
        assert len(d.steps) == 2
        assert d.steps[0].name == "step1"


# ── ProcessEngine: register & list ──────────────────────────────────────────


class TestProcessEngineRegisterList:
    def test_register_and_list(self):
        engine = ProcessEngine(FakeStorage())
        engine.register_process(ProcessDefinition(name="alpha"))
        engine.register_process(ProcessDefinition(name="beta"))
        names = engine.list_definitions()
        assert "alpha" in names
        assert "beta" in names

    def test_defaults_registered(self):
        engine = ProcessEngine(FakeStorage())
        names = engine.list_definitions()
        for expected in [
            "chat_session",
            "research_task",
            "artifact_generation",
            "review_task",
            "workspace_task",
        ]:
            assert expected in names


# ── ProcessEngine: select ────────────────────────────────────────────────────


class TestProcessEngineSelect:
    def test_select_matching(self):
        engine = ProcessEngine(FakeStorage())
        defn = ProcessDefinition(name="chat_session")
        engine.register_process(defn)
        result = engine.select("chat_session")
        assert result is not None
        assert result.name == "chat_session"

    def test_select_no_match(self):
        engine = ProcessEngine(FakeStorage())
        engine.register_process(ProcessDefinition(name="chat_session"))
        assert engine.select("nonexistent") is None


# ── ProcessEngine: start ─────────────────────────────────────────────────────


class TestProcessEngineStart:
    def test_start_creates_running_instance(self):
        engine = ProcessEngine(FakeStorage())
        defn = ProcessDefinition(
            name="flow",
            steps=[ProcessStep(name="s1"), ProcessStep(name="s2")],
        )
        engine.register_process(defn)
        instance = engine.start("sess-1", "flow")
        assert isinstance(instance, ProcessInstance)
        assert instance.state == "running"
        assert instance.session_id == "sess-1"
        assert instance.process_name == "flow"
        assert instance.current_step == "s1"


# ── ProcessEngine: advance ───────────────────────────────────────────────────


class TestProcessEngineAdvance:
    def _make_engine(self) -> tuple[ProcessEngine, ProcessInstance]:
        storage = FakeStorage()
        engine = ProcessEngine(storage)
        defn = ProcessDefinition(
            name="flow",
            steps=[
                ProcessStep(name="s1"),
                ProcessStep(name="s2"),
                ProcessStep(name="s3"),
            ],
        )
        engine.register_process(defn)
        instance = engine.start("sess-1", "flow")
        return engine, instance

    def test_advance_moves_to_next_step(self):
        engine, instance = self._make_engine()
        updated = engine.advance(instance.id)
        assert updated.current_step == "s2"
        assert updated.state == "running"

    def test_advance_logs_each_step(self):
        engine, instance = self._make_engine()
        updated = engine.advance(instance.id)
        assert len(updated.logs) >= 1
        assert updated.logs[-1]["step"] == "s1"

    def test_advance_past_last_step_completes(self):
        engine, instance = self._make_engine()
        engine.advance(instance.id)  # s1 -> s2
        engine.advance(instance.id)  # s2 -> s3
        final = engine.advance(instance.id)  # s3 -> completed
        assert final.state == "completed"

    def test_advance_nonexistent_raises(self):
        engine = ProcessEngine(FakeStorage())
        with pytest.raises(EntityNotFoundError):
            engine.advance("no-such-id")

    def test_advance_completed_raises(self):
        storage = FakeStorage()
        engine = ProcessEngine(storage)
        defn = ProcessDefinition(
            name="short", steps=[ProcessStep(name="only")]
        )
        engine.register_process(defn)
        inst = engine.start("sess-1", "short")
        engine.advance(inst.id)  # completes the process
        assert inst.state == "completed"
        with pytest.raises(ValidationError, match="expected 'running'"):
            engine.advance(inst.id)

    def test_start_unregistered_process_raises(self):
        engine = ProcessEngine(FakeStorage())
        with pytest.raises(EntityNotFoundError, match="not found"):
            engine.start("sess-1", "nonexistent_process")


# ── ProcessEngine: get_instance ──────────────────────────────────────────────


class TestProcessEngineGetInstance:
    def test_get_existing(self):
        storage = FakeStorage()
        engine = ProcessEngine(storage)
        defn = ProcessDefinition(
            name="flow", steps=[ProcessStep(name="s1")]
        )
        engine.register_process(defn)
        instance = engine.start("sess-1", "flow")
        fetched = engine.get_instance(instance.id)
        assert fetched is not None
        assert fetched.id == instance.id

    def test_get_nonexistent(self):
        engine = ProcessEngine(FakeStorage())
        assert engine.get_instance("nope") is None


# ── ProcessRunner ────────────────────────────────────────────────────────────


class TestProcessRunner:
    def _make_runner(self) -> ProcessRunner:
        storage = FakeStorage()
        engine = ProcessEngine(storage)
        engine.register_process(
            ProcessDefinition(
                name="chat_session",
                steps=[
                    ProcessStep(name="greet"),
                    ProcessStep(name="respond"),
                ],
            )
        )
        return ProcessRunner(engine)

    def test_runner_type(self):
        runner = self._make_runner()
        assert runner.runner_type == "process"

    def test_can_handle_registered(self):
        runner = self._make_runner()
        assert runner.can_handle("chat_session") is True

    def test_can_handle_unregistered(self):
        runner = self._make_runner()
        assert runner.can_handle("nonexistent") is False

    def test_run_completes_process(self):
        runner = self._make_runner()
        ctx = AssembledContext(
            symbiote_id="s1",
            session_id="sess-1",
            user_input="chat_session",
        )
        result = runner.run(ctx)
        assert isinstance(result, RunResult)
        assert result.success is True
        assert result.runner_type == "process"


# ── Cache Invalidation (B-6) ────────────────────────────────────────────────


class TestCacheInvalidation:
    def _make_engine(self) -> ProcessEngine:
        engine = ProcessEngine(FakeStorage())
        engine.register_process(
            ProcessDefinition(
                name="flow",
                steps=[ProcessStep(name="s1"), ProcessStep(name="s2")],
            )
        )
        return engine

    def test_invalidate_single_clears_from_cache(self):
        engine = self._make_engine()
        inst = engine.start("sess-1", "flow")
        assert inst.id in engine._instances

        engine.invalidate_cache(inst.id)
        assert inst.id not in engine._instances
        assert inst.id not in engine._instance_step_index
        assert inst.id not in engine._cache_timestamps

    def test_invalidate_all_clears_cache(self):
        engine = self._make_engine()
        engine.start("sess-1", "flow")
        engine.start("sess-2", "flow")
        assert len(engine._instances) == 2

        engine.invalidate_cache()
        assert len(engine._instances) == 0
        assert len(engine._instance_step_index) == 0
        assert len(engine._cache_timestamps) == 0

    def test_cache_repopulated_after_invalidation(self):
        """After invalidation, get_instance re-fetches from storage."""
        import tempfile
        from pathlib import Path
        from uuid import uuid4

        from symbiote.adapters.storage.sqlite import SQLiteAdapter
        from symbiote.core.identity import IdentityManager
        from symbiote.core.session import SessionManager

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "cache_test.db"
            adapter = SQLiteAdapter(db_path=db)
            adapter.init_schema()

            # Create symbiote and session for FK
            sym = IdentityManager(storage=adapter).create(name="B", role="a")
            sess = SessionManager(adapter).start(symbiote_id=sym.id)

            engine = ProcessEngine(adapter)
            engine.register_process(
                ProcessDefinition(
                    name="flow",
                    steps=[ProcessStep(name="s1"), ProcessStep(name="s2")],
                )
            )
            inst = engine.start(sess.id, "flow")
            engine.advance(inst.id)

            engine.invalidate_cache(inst.id)

            refetched = engine.get_instance(inst.id)
            assert refetched is not None
            assert refetched.current_step == "s2"

            adapter.close()

    def test_stale_cache_triggers_refetch(self):
        """Instances with expired TTL trigger DB re-fetch."""
        import tempfile
        from pathlib import Path

        from symbiote.adapters.storage.sqlite import SQLiteAdapter
        from symbiote.core.identity import IdentityManager
        from symbiote.core.session import SessionManager

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "ttl_test.db"
            adapter = SQLiteAdapter(db_path=db)
            adapter.init_schema()

            sym = IdentityManager(storage=adapter).create(name="B", role="a")
            sess = SessionManager(adapter).start(symbiote_id=sym.id)

            engine = ProcessEngine(adapter, cache_ttl=0.0)
            engine.register_process(
                ProcessDefinition(
                    name="flow",
                    steps=[ProcessStep(name="s1")],
                )
            )
            inst = engine.start(sess.id, "flow")

            # TTL=0 → always stale → refetch from DB
            refetched = engine.get_instance(inst.id)
            assert refetched is not None
            assert refetched.id == inst.id

            adapter.close()

    def test_advance_refreshes_cache(self):
        engine = self._make_engine()
        inst = engine.start("sess-1", "flow")
        assert inst.current_step == "s1"

        advanced = engine.advance(inst.id)
        assert advanced.current_step == "s2"

        # Cache should have the updated instance
        cached = engine.get_instance(inst.id)
        assert cached.current_step == "s2"
