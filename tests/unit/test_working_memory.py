"""Unit tests for WorkingMemory helper."""

from __future__ import annotations

import pytest

from symbiote.core.models import Decision, Message
from symbiote.memory.working import WorkingMemory

SESSION_ID = "sess-001"


def _msg(role: str = "user", content: str = "hello") -> Message:
    return Message(session_id=SESSION_ID, role=role, content=content)


def _decision(title: str = "Use pytest", desc: str = "Standard test runner") -> Decision:
    return Decision(session_id=SESSION_ID, title=title, description=desc)


# ── Initial state ────────────────────────────────────────────────────────────


class TestInitialState:
    def test_empty_messages(self) -> None:
        wm = WorkingMemory(SESSION_ID)
        assert wm.recent_messages == []

    def test_no_goal(self) -> None:
        wm = WorkingMemory(SESSION_ID)
        assert wm.current_goal is None

    def test_no_plan(self) -> None:
        wm = WorkingMemory(SESSION_ID)
        assert wm.active_plan is None

    def test_no_files(self) -> None:
        wm = WorkingMemory(SESSION_ID)
        assert wm.active_files == []

    def test_no_decisions(self) -> None:
        wm = WorkingMemory(SESSION_ID)
        assert wm.recent_decisions == []

    def test_session_id_stored(self) -> None:
        wm = WorkingMemory(SESSION_ID)
        assert wm.session_id == SESSION_ID


# ── update_message ───────────────────────────────────────────────────────────


class TestUpdateMessage:
    def test_appends_message(self) -> None:
        wm = WorkingMemory(SESSION_ID)
        msg = _msg()
        wm.update_message(msg)
        assert len(wm.recent_messages) == 1
        assert wm.recent_messages[0] is msg

    def test_trims_to_max_messages(self) -> None:
        wm = WorkingMemory(SESSION_ID, max_messages=3)
        # All user messages — trim is straightforward
        msgs = [_msg(role="user", content=f"m{i}") for i in range(5)]
        for m in msgs:
            wm.update_message(m)
        assert len(wm.recent_messages) == 3
        assert wm.recent_messages[0].content == "m2"
        assert wm.recent_messages[2].content == "m4"

    def test_trim_aligns_to_user_turn(self) -> None:
        """B-18: Trimming should not orphan assistant messages."""
        wm = WorkingMemory(SESSION_ID, max_messages=4)
        # user, assistant, user, assistant, user, assistant
        for i in range(3):
            wm.update_message(_msg(role="user", content=f"u{i}"))
            wm.update_message(_msg(role="assistant", content=f"a{i}"))
        # 6 messages, max 4 → raw trim gives [assistant, user, assistant, user? no]
        # raw: [-4:] = a1, u2, a2 — wait, 6 msgs, [-4:] = u1, a1, u2, a2
        # Actually with 6 msgs and max 4: [-4:] = [u1, a1, u2, a2] which starts with user ✓
        # Let me use max_messages=3 to force mid-turn cut
        wm2 = WorkingMemory(SESSION_ID, max_messages=3)
        wm2.update_message(_msg(role="user", content="u0"))
        wm2.update_message(_msg(role="assistant", content="a0"))
        wm2.update_message(_msg(role="user", content="u1"))
        wm2.update_message(_msg(role="assistant", content="a1"))
        # 4 msgs, max 3: [-3:] = [a0, u1, a1] → starts with assistant → align drops a0
        assert wm2.recent_messages[0].role == "user"
        assert wm2.recent_messages[0].content == "u1"

    def test_trim_preserves_all_user_messages(self) -> None:
        """B-18: All-user messages trim normally."""
        wm = WorkingMemory(SESSION_ID, max_messages=2)
        for i in range(5):
            wm.update_message(_msg(role="user", content=f"u{i}"))
        assert len(wm.recent_messages) == 2
        assert wm.recent_messages[0].content == "u3"

    def test_trim_all_assistant_returns_asis(self) -> None:
        """B-18: Edge case — all assistant messages returned as-is."""
        wm = WorkingMemory(SESSION_ID, max_messages=2)
        for i in range(5):
            wm.update_message(_msg(role="assistant", content=f"a{i}"))
        # No user message found — return as-is
        assert len(wm.recent_messages) == 2


# ── update_goal / update_plan ────────────────────────────────────────────────


class TestGoalAndPlan:
    def test_update_goal(self) -> None:
        wm = WorkingMemory(SESSION_ID)
        wm.update_goal("Ship v1")
        assert wm.current_goal == "Ship v1"

    def test_update_plan(self) -> None:
        wm = WorkingMemory(SESSION_ID)
        wm.update_plan("Step 1: write tests")
        assert wm.active_plan == "Step 1: write tests"


# ── active_files ─────────────────────────────────────────────────────────────


class TestActiveFiles:
    def test_add_file(self) -> None:
        wm = WorkingMemory(SESSION_ID)
        wm.add_active_file("src/main.py")
        assert "src/main.py" in wm.active_files

    def test_no_duplicates(self) -> None:
        wm = WorkingMemory(SESSION_ID)
        wm.add_active_file("src/main.py")
        wm.add_active_file("src/main.py")
        assert wm.active_files.count("src/main.py") == 1

    def test_remove_file(self) -> None:
        wm = WorkingMemory(SESSION_ID)
        wm.add_active_file("src/main.py")
        wm.remove_active_file("src/main.py")
        assert wm.active_files == []

    def test_remove_missing_is_noop(self) -> None:
        wm = WorkingMemory(SESSION_ID)
        wm.remove_active_file("nonexistent.py")  # should not raise
        assert wm.active_files == []


# ── add_decision ─────────────────────────────────────────────────────────────


class TestAddDecision:
    def test_appends_decision(self) -> None:
        wm = WorkingMemory(SESSION_ID)
        dec = _decision()
        wm.add_decision(dec)
        assert len(wm.recent_decisions) == 1
        assert wm.recent_decisions[0] is dec


# ── snapshot ─────────────────────────────────────────────────────────────────


class TestSnapshot:
    def test_snapshot_structure(self) -> None:
        wm = WorkingMemory(SESSION_ID)
        wm.update_message(_msg(role="user", content="hi"))
        wm.update_goal("goal-1")
        wm.update_plan("plan-1")
        wm.add_active_file("a.py")
        wm.add_decision(_decision(title="D1", desc="desc1"))

        snap = wm.snapshot()

        assert snap["session_id"] == SESSION_ID
        assert snap["current_goal"] == "goal-1"
        assert snap["active_plan"] == "plan-1"
        assert snap["active_files"] == ["a.py"]

        # Messages serialised as dicts with role+content
        assert len(snap["recent_messages"]) == 1
        assert snap["recent_messages"][0]["role"] == "user"
        assert snap["recent_messages"][0]["content"] == "hi"

        # Decisions serialised as dicts with title+description
        assert len(snap["recent_decisions"]) == 1
        assert snap["recent_decisions"][0]["title"] == "D1"
        assert snap["recent_decisions"][0]["description"] == "desc1"

    def test_snapshot_empty_state(self) -> None:
        wm = WorkingMemory(SESSION_ID)
        snap = wm.snapshot()
        assert snap == {
            "session_id": SESSION_ID,
            "recent_messages": [],
            "current_goal": None,
            "active_plan": None,
            "active_files": [],
            "recent_decisions": [],
        }


# ── clear ────────────────────────────────────────────────────────────────────


class TestClear:
    def test_clear_resets_state(self) -> None:
        wm = WorkingMemory(SESSION_ID)
        wm.update_message(_msg())
        wm.update_goal("g")
        wm.update_plan("p")
        wm.add_active_file("f.py")
        wm.add_decision(_decision())

        wm.clear()

        assert wm.recent_messages == []
        assert wm.current_goal is None
        assert wm.active_plan is None
        assert wm.active_files == []
        assert wm.recent_decisions == []

    def test_clear_preserves_session_id(self) -> None:
        wm = WorkingMemory(SESSION_ID)
        wm.clear()
        assert wm.session_id == SESSION_ID
