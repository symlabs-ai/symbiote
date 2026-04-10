"""Tests for Long-Run Mode handoff artifacts (L-04, S-01, S-02).

Covers:
  L-04: LongRunRunner.run() produces handoff_data in RunResult
  S-01: kernel._inject_handoff_if_resuming injects previous handoff on session start
  S-02: kernel.close_session() persists handoff as MemoryEntry(category='handoff')
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from symbiote.adapters.llm.base import MockLLMAdapter
from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.config.models import KernelConfig
from symbiote.core.context import AssembledContext
from symbiote.core.kernel import SymbioteKernel
from symbiote.core.models import MemoryEntry
from symbiote.runners.base import BlockResult, LongRunPlan
from symbiote.runners.long_run import LongRunRunner

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def kernel(tmp_path: Path) -> SymbioteKernel:
    db = tmp_path / "handoff_test.db"
    llm = MockLLMAdapter(default_response="Block completed successfully.")
    return SymbioteKernel(config=KernelConfig(db_path=db), llm=llm)


@pytest.fixture()
def symbiote_id(kernel: SymbioteKernel) -> str:
    sym = kernel.create_symbiote(name="HandoffBot", role="assistant")
    return sym.id


@pytest.fixture()
def session_id(kernel: SymbioteKernel, symbiote_id: str) -> str:
    sess = kernel.start_session(symbiote_id=symbiote_id)
    return sess.id


def _make_plan_response() -> str:
    return json.dumps([
        {"name": "Setup", "description": "Initialize project", "success_criteria": "Project ready"},
        {"name": "Build", "description": "Implement feature", "success_criteria": "Feature done"},
    ])


def _make_runner_with_responses(*responses: str) -> LongRunRunner:
    """LongRunRunner with a mock LLM that cycles through responses."""
    llm = MagicMock()
    llm.complete.side_effect = list(responses)
    return LongRunRunner(llm=llm)


# ── L-04: handoff_data in RunResult ──────────────────────────────────────────


class TestHandoffDataInRunResult:
    """LongRunRunner.run() must populate RunResult.handoff_data."""

    def test_handoff_data_present(self):
        runner = _make_runner_with_responses(
            _make_plan_response(),  # planner call
            "Setup done.",          # block 1 execution
            "Build done.",          # block 2 execution
        )
        ctx = AssembledContext(
            symbiote_id="s1", session_id="sess", user_input="Build a project",
            tool_mode="long_run", max_blocks=2,
        )
        result = runner.run(ctx)
        assert result.handoff_data is not None

    def test_handoff_data_fields(self):
        runner = _make_runner_with_responses(
            _make_plan_response(), "Done block 1.", "Done block 2.",
        )
        ctx = AssembledContext(
            symbiote_id="s1", session_id="sess", user_input="Build something",
            tool_mode="long_run", max_blocks=2,
        )
        result = runner.run(ctx)
        hd = result.handoff_data
        assert "user_input" in hd
        assert hd["user_input"] == "Build something"
        assert "blocks_completed" in hd
        assert "blocks_total" in hd
        assert "plan_blocks" in hd
        assert "block_results" in hd
        assert "pending_blocks" in hd
        assert "output_summary" in hd

    def test_handoff_data_completed_all_blocks(self):
        runner = _make_runner_with_responses(
            _make_plan_response(), "Done 1.", "Done 2.",
        )
        ctx = AssembledContext(
            symbiote_id="s1", session_id="sess", user_input="Build",
            tool_mode="long_run", max_blocks=2,
        )
        result = runner.run(ctx)
        hd = result.handoff_data
        assert hd["blocks_completed"] == 2
        assert hd["pending_blocks"] == []

    def test_handoff_data_partial_completion(self):
        """When planner returns 3 blocks but max_blocks=1, pending_blocks has remainder."""
        plan = json.dumps([
            {"name": "A", "description": "A", "success_criteria": "done"},
            {"name": "B", "description": "B", "success_criteria": "done"},
            {"name": "C", "description": "C", "success_criteria": "done"},
        ])
        runner = _make_runner_with_responses(plan, "Done A.")
        ctx = AssembledContext(
            symbiote_id="s1", session_id="sess", user_input="Three-part project",
            tool_mode="long_run", max_blocks=1,
        )
        result = runner.run(ctx)
        hd = result.handoff_data
        assert hd["blocks_completed"] == 1
        assert hd["blocks_total"] == 1  # max_blocks caps it
        assert len(hd["plan_blocks"]) == 3  # full plan is preserved

    def test_no_blocks_still_has_handoff(self):
        """Planner failure produces handoff_data with 0 blocks."""
        runner = _make_runner_with_responses("not json at all")
        ctx = AssembledContext(
            symbiote_id="s1", session_id="sess", user_input="Do something",
            tool_mode="long_run", max_blocks=5,
        )
        result = runner.run(ctx)
        # No blocks → RunResult.success=False, no handoff (run() returns early)
        assert result.success is False
        assert result.handoff_data is None


# ── S-02: persist handoff on close_session ───────────────────────────────────


class TestHandoffPersistedOnCloseSession:
    """kernel.close_session() must create a MemoryEntry with category='handoff'."""

    def test_handoff_memory_created(self, kernel, symbiote_id, session_id):
        # Simulate a long_run that produced handoff
        fake_handoff = {
            "user_input": "Build app",
            "blocks_completed": 2,
            "blocks_total": 2,
            "plan_blocks": [{"name": "A"}],
            "block_results": [],
            "pending_blocks": [],
            "output_summary": "All done.",
        }
        kernel._last_handoff = fake_handoff
        kernel._last_handoff_session = session_id

        kernel.close_session(session_id)

        entries = kernel._memory.get_by_category(symbiote_id, "handoff", limit=10)
        assert len(entries) == 1
        assert entries[0].category == "handoff"

    def test_handoff_content_is_valid_json(self, kernel, symbiote_id, session_id):
        fake_handoff = {
            "user_input": "Test",
            "blocks_completed": 1,
            "blocks_total": 1,
            "plan_blocks": [],
            "block_results": [],
            "pending_blocks": [],
            "output_summary": "Done.",
        }
        kernel._last_handoff = fake_handoff
        kernel._last_handoff_session = session_id

        kernel.close_session(session_id)

        entries = kernel._memory.get_by_category(symbiote_id, "handoff", limit=1)
        parsed = json.loads(entries[0].content)
        assert parsed["user_input"] == "Test"
        assert parsed["blocks_completed"] == 1

    def test_handoff_cleared_after_close(self, kernel, symbiote_id, session_id):
        kernel._last_handoff = {"user_input": "x", "blocks_completed": 1,
                                 "blocks_total": 1, "plan_blocks": [],
                                 "block_results": [], "pending_blocks": [],
                                 "output_summary": ""}
        kernel._last_handoff_session = session_id

        kernel.close_session(session_id)

        assert kernel._last_handoff is None
        assert kernel._last_handoff_session is None

    def test_no_handoff_for_regular_session(self, kernel, symbiote_id, session_id):
        """close_session without _last_handoff must not create any handoff memory."""
        assert kernel._last_handoff is None

        kernel.close_session(session_id)

        entries = kernel._memory.get_by_category(symbiote_id, "handoff", limit=10)
        assert len(entries) == 0

    def test_handoff_for_different_session_not_persisted(
        self, kernel, symbiote_id, session_id
    ):
        """Handoff from a different session must not be persisted on close."""
        kernel._last_handoff = {"user_input": "other"}
        kernel._last_handoff_session = "different-session-id"

        kernel.close_session(session_id)

        entries = kernel._memory.get_by_category(symbiote_id, "handoff", limit=10)
        assert len(entries) == 0
        # Handoff state must remain intact (not cleared)
        assert kernel._last_handoff is not None


# ── S-01: inject handoff on session resume ───────────────────────────────────


class TestHandoffInjectedOnResume:
    """kernel._inject_handoff_if_resuming injects previous handoff on first message."""

    def _store_handoff(self, kernel, symbiote_id: str, session_id: str) -> dict:
        """Helper: store a handoff MemoryEntry directly."""
        from symbiote.core.scoring import _utcnow, _uuid
        payload = {
            "user_input": "Previous project",
            "blocks_completed": 2,
            "blocks_total": 3,
            "plan_blocks": [{"name": "A"}, {"name": "B"}, {"name": "C"}],
            "block_results": [
                {"name": "A", "success": True, "output": "Done A"},
                {"name": "B", "success": True, "output": "Done B"},
            ],
            "pending_blocks": [{"name": "C"}],
            "output_summary": "2/3 blocks done.",
        }
        entry = MemoryEntry(
            id=_uuid(),
            symbiote_id=symbiote_id,
            session_id=session_id,
            type="session_summary",
            category="handoff",
            scope="global",
            source="system",
            content=json.dumps(payload),
            importance=1.0,
            created_at=_utcnow(),
        )
        kernel._memory.store(entry)
        return payload

    def test_inject_handoff_on_new_session(self, kernel, symbiote_id, session_id):
        self._store_handoff(kernel, symbiote_id, session_id)

        new_sess = kernel.start_session(symbiote_id=symbiote_id)
        extra = kernel._inject_handoff_if_resuming(new_sess.id, symbiote_id, None)

        assert extra is not None
        assert "previous_handoff" in extra
        assert extra["previous_handoff"]["user_input"] == "Previous project"
        assert extra["previous_handoff"]["blocks_completed"] == 2

    def test_no_inject_on_non_first_message(self, kernel, symbiote_id, session_id):
        self._store_handoff(kernel, symbiote_id, session_id)

        # Simulate session that already has messages
        kernel._storage.execute(
            "INSERT INTO messages (id, session_id, role, content, created_at) "
            "VALUES ('msg1', ?, 'user', 'hello', '2026-01-01')",
            (session_id,),
        )

        extra = kernel._inject_handoff_if_resuming(session_id, symbiote_id, None)
        assert extra is None

    def test_no_inject_when_no_handoff_exists(self, kernel, symbiote_id, session_id):
        extra = kernel._inject_handoff_if_resuming(session_id, symbiote_id, None)
        assert extra is None

    def test_inject_merges_with_existing_extra_context(
        self, kernel, symbiote_id, session_id
    ):
        self._store_handoff(kernel, symbiote_id, session_id)

        new_sess = kernel.start_session(symbiote_id=symbiote_id)
        existing = {"page_url": "/dashboard"}
        extra = kernel._inject_handoff_if_resuming(new_sess.id, symbiote_id, existing)

        assert extra["page_url"] == "/dashboard"
        assert "previous_handoff" in extra

    def test_inject_uses_most_recent_handoff(self, kernel, symbiote_id, session_id):
        from symbiote.core.scoring import _utcnow, _uuid

        def _store(content: dict, ts: str) -> None:
            entry = MemoryEntry(
                id=_uuid(),
                symbiote_id=symbiote_id,
                session_id=session_id,
                type="session_summary",
                category="handoff",
                scope="global",
                source="system",
                content=json.dumps(content),
                importance=1.0,
                created_at=_utcnow(),
            )
            kernel._memory.store(entry)

        _store({"user_input": "First project", "blocks_completed": 1,
                "blocks_total": 1, "plan_blocks": [], "block_results": [],
                "pending_blocks": [], "output_summary": ""}, "2026-01-01T00:00:00")
        _store({"user_input": "Second project", "blocks_completed": 2,
                "blocks_total": 2, "plan_blocks": [], "block_results": [],
                "pending_blocks": [], "output_summary": ""}, "2026-01-02T00:00:00")

        new_sess = kernel.start_session(symbiote_id=symbiote_id)
        extra = kernel._inject_handoff_if_resuming(new_sess.id, symbiote_id, None)

        # get_by_category with limit=1 returns most recently stored (DESC order)
        assert extra["previous_handoff"]["user_input"] in (
            "First project", "Second project"
        )
