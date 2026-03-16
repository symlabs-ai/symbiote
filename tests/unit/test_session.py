"""Tests for SessionManager — T-05."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.exceptions import EntityNotFoundError
from symbiote.core.models import Decision, Message, Session
from symbiote.core.session import SessionManager


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "session_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def manager(adapter: SQLiteAdapter) -> SessionManager:
    return SessionManager(storage=adapter)


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    """Insert a minimal symbiote row and return its id."""
    sid = "sym-test-001"
    adapter.execute(
        "INSERT INTO symbiotes (id, name, role, created_at, updated_at) "
        "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
        (sid, "TestBot", "assistant"),
    )
    return sid


# ── Start ──────────────────────────────────────────────────────────────────


class TestStart:
    def test_start_returns_active_session(
        self, manager: SessionManager, symbiote_id: str
    ) -> None:
        session = manager.start(symbiote_id)
        assert isinstance(session, Session)
        assert session.symbiote_id == symbiote_id
        assert session.status == "active"
        assert session.id

    def test_start_persisted_and_retrievable(
        self, manager: SessionManager, symbiote_id: str
    ) -> None:
        session = manager.start(symbiote_id)
        fetched = manager.resume(session.id)
        assert fetched is not None
        assert fetched.id == session.id
        assert fetched.symbiote_id == symbiote_id

    def test_start_with_goal_and_workspace(
        self, manager: SessionManager, symbiote_id: str
    ) -> None:
        session = manager.start(
            symbiote_id, goal="Fix bug #42", workspace_id="ws-001"
        )
        assert session.goal == "Fix bug #42"
        assert session.workspace_id == "ws-001"

        fetched = manager.resume(session.id)
        assert fetched is not None
        assert fetched.goal == "Fix bug #42"
        assert fetched.workspace_id == "ws-001"


# ── Resume ─────────────────────────────────────────────────────────────────


class TestResume:
    def test_resume_existing(
        self, manager: SessionManager, symbiote_id: str
    ) -> None:
        session = manager.start(symbiote_id)
        fetched = manager.resume(session.id)
        assert fetched is not None
        assert fetched.id == session.id

    def test_resume_nonexistent_returns_none(
        self, manager: SessionManager
    ) -> None:
        assert manager.resume("ghost-id") is None

    def test_resume_closed_session_reopens(
        self, manager: SessionManager, symbiote_id: str
    ) -> None:
        session = manager.start(symbiote_id)
        manager.close(session.id)

        reopened = manager.resume(session.id)
        assert reopened is not None
        assert reopened.status == "active"
        assert reopened.ended_at is None


# ── Close ──────────────────────────────────────────────────────────────────


class TestClose:
    def test_close_sets_status_and_ended_at(
        self, manager: SessionManager, symbiote_id: str
    ) -> None:
        session = manager.start(symbiote_id)
        closed = manager.close(session.id)
        assert closed.status == "closed"
        assert closed.ended_at is not None

    def test_close_generates_summary_no_messages(
        self, manager: SessionManager, symbiote_id: str
    ) -> None:
        session = manager.start(symbiote_id)
        closed = manager.close(session.id)
        assert closed.summary == "No messages"

    def test_close_generates_summary_with_messages(
        self, manager: SessionManager, symbiote_id: str
    ) -> None:
        session = manager.start(symbiote_id)
        for i in range(7):
            manager.add_message(session.id, "user", f"msg-{i}")
        closed = manager.close(session.id)
        # Summary should contain last 5 messages (msg-2..msg-6)
        assert closed.summary is not None
        assert "msg-1" not in closed.summary  # older than last 5
        assert "msg-0" not in closed.summary  # older than last 5
        for i in range(2, 7):
            assert f"msg-{i}" in closed.summary

    def test_close_nonexistent_raises(self, manager: SessionManager) -> None:
        with pytest.raises(EntityNotFoundError, match="not found"):
            manager.close("ghost-id")


# ── Add Message ────────────────────────────────────────────────────────────


class TestAddMessage:
    def test_add_message_returns_message(
        self, manager: SessionManager, symbiote_id: str
    ) -> None:
        session = manager.start(symbiote_id)
        msg = manager.add_message(session.id, "user", "Hello!")
        assert isinstance(msg, Message)
        assert msg.session_id == session.id
        assert msg.role == "user"
        assert msg.content == "Hello!"

    def test_add_message_persisted(
        self, manager: SessionManager, symbiote_id: str
    ) -> None:
        session = manager.start(symbiote_id)
        msg = manager.add_message(session.id, "assistant", "Hi there")
        messages = manager.get_messages(session.id)
        assert len(messages) == 1
        assert messages[0].id == msg.id
        assert messages[0].content == "Hi there"

    def test_add_message_nonexistent_session_raises(
        self, manager: SessionManager
    ) -> None:
        with pytest.raises(EntityNotFoundError, match="not found"):
            manager.add_message("ghost-id", "user", "Hello!")


# ── Get Messages ───────────────────────────────────────────────────────────


class TestGetMessages:
    def test_get_messages_ordered_by_created_at_desc(
        self, manager: SessionManager, symbiote_id: str
    ) -> None:
        session = manager.start(symbiote_id)
        for i in range(5):
            manager.add_message(session.id, "user", f"msg-{i}")
            time.sleep(0.01)  # ensure distinct timestamps
        messages = manager.get_messages(session.id)
        assert len(messages) == 5
        # Most recent first
        assert messages[0].content == "msg-4"
        assert messages[4].content == "msg-0"

    def test_get_messages_respects_limit(
        self, manager: SessionManager, symbiote_id: str
    ) -> None:
        session = manager.start(symbiote_id)
        for i in range(10):
            manager.add_message(session.id, "user", f"msg-{i}")
        messages = manager.get_messages(session.id, limit=3)
        assert len(messages) == 3

    def test_get_messages_empty_session(
        self, manager: SessionManager, symbiote_id: str
    ) -> None:
        session = manager.start(symbiote_id)
        messages = manager.get_messages(session.id)
        assert messages == []


# ── Add Decision ───────────────────────────────────────────────────────────


class TestAddDecision:
    def test_add_decision_returns_decision(
        self, manager: SessionManager, symbiote_id: str
    ) -> None:
        session = manager.start(symbiote_id)
        dec = manager.add_decision(
            session.id,
            title="Use SQLite",
            description="Lightweight and embedded",
            tags=["architecture", "storage"],
        )
        assert isinstance(dec, Decision)
        assert dec.session_id == session.id
        assert dec.title == "Use SQLite"
        assert dec.description == "Lightweight and embedded"
        assert dec.tags == ["architecture", "storage"]

    def test_add_decision_persisted_with_tags(
        self, manager: SessionManager, symbiote_id: str
    ) -> None:
        session = manager.start(symbiote_id)
        manager.add_decision(
            session.id, title="Go async", tags=["design"]
        )
        decisions = manager.get_decisions(session.id)
        assert len(decisions) == 1
        assert decisions[0].title == "Go async"
        assert decisions[0].tags == ["design"]

    def test_add_decision_nonexistent_session_raises(
        self, manager: SessionManager
    ) -> None:
        with pytest.raises(EntityNotFoundError, match="not found"):
            manager.add_decision("ghost-id", title="Nope")


# ── Get Decisions ──────────────────────────────────────────────────────────


class TestGetDecisions:
    def test_get_decisions_returns_all(
        self, manager: SessionManager, symbiote_id: str
    ) -> None:
        session = manager.start(symbiote_id)
        manager.add_decision(session.id, title="Dec 1")
        manager.add_decision(session.id, title="Dec 2")
        manager.add_decision(session.id, title="Dec 3")
        decisions = manager.get_decisions(session.id)
        assert len(decisions) == 3
        titles = {d.title for d in decisions}
        assert titles == {"Dec 1", "Dec 2", "Dec 3"}

    def test_get_decisions_empty(
        self, manager: SessionManager, symbiote_id: str
    ) -> None:
        session = manager.start(symbiote_id)
        assert manager.get_decisions(session.id) == []
