"""Tests for MessageRepository — B-3."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from symbiote.adapters.storage.message_repository import MessageRepository
from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.identity import IdentityManager


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "msg_repo_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def session_id(adapter: SQLiteAdapter) -> str:
    sym_id = IdentityManager(storage=adapter).create(name="Bot", role="assistant").id
    sid = str(uuid4())
    adapter.execute(
        "INSERT INTO sessions (id, symbiote_id, status, started_at) "
        "VALUES (?, ?, 'active', datetime('now'))",
        (sid, sym_id),
    )
    return sid


@pytest.fixture()
def repo(adapter: SQLiteAdapter) -> MessageRepository:
    return MessageRepository(adapter)


_seq = 0


def _insert(adapter: SQLiteAdapter, session_id: str, role: str, content: str) -> None:
    global _seq
    _seq += 1
    adapter.execute(
        "INSERT INTO messages (id, session_id, role, content, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (str(uuid4()), session_id, role, content, f"2026-01-01T00:00:{_seq:02d}"),
    )


class TestMessageRepository:
    def test_returns_empty_for_no_messages(
        self, repo: MessageRepository, session_id: str
    ) -> None:
        assert repo.get_messages(session_id) == []

    def test_returns_messages_chronological(
        self, repo: MessageRepository, adapter: SQLiteAdapter, session_id: str
    ) -> None:
        _insert(adapter, session_id, "user", "first")
        _insert(adapter, session_id, "assistant", "second")
        _insert(adapter, session_id, "user", "third")

        msgs = repo.get_messages(session_id)
        assert len(msgs) == 3
        assert msgs[0]["content"] == "first"
        assert msgs[1]["content"] == "second"
        assert msgs[2]["content"] == "third"

    def test_respects_limit(
        self, repo: MessageRepository, adapter: SQLiteAdapter, session_id: str
    ) -> None:
        for i in range(10):
            _insert(adapter, session_id, "user", f"msg-{i}")

        msgs = repo.get_messages(session_id, limit=3)
        assert len(msgs) == 3

    def test_has_role_and_content_keys(
        self, repo: MessageRepository, adapter: SQLiteAdapter, session_id: str
    ) -> None:
        _insert(adapter, session_id, "user", "hello")
        msgs = repo.get_messages(session_id)
        assert "role" in msgs[0]
        assert "content" in msgs[0]
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "hello"
