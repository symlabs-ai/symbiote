"""Tests for Session external_key feature."""

from __future__ import annotations

from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.identity import IdentityManager
from symbiote.core.session import SessionManager


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "ext_key_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name="KeyBot", role="assistant")
    return sym.id


@pytest.fixture()
def sessions(adapter: SQLiteAdapter) -> SessionManager:
    return SessionManager(storage=adapter)


class TestStartWithExternalKey:
    def test_start_with_key(self, sessions: SessionManager, symbiote_id: str) -> None:
        sess = sessions.start(symbiote_id=symbiote_id, external_key="user1:homepage")
        assert sess.external_key == "user1:homepage"

    def test_start_without_key(self, sessions: SessionManager, symbiote_id: str) -> None:
        sess = sessions.start(symbiote_id=symbiote_id)
        assert sess.external_key is None

    def test_key_persists_on_resume(self, sessions: SessionManager, symbiote_id: str) -> None:
        sess = sessions.start(symbiote_id=symbiote_id, external_key="user1:page")
        resumed = sessions.resume(sess.id)
        assert resumed is not None
        assert resumed.external_key == "user1:page"


class TestGetOrCreateByExternalKey:
    def test_creates_new_when_none_exists(self, sessions: SessionManager, symbiote_id: str) -> None:
        sess = sessions.get_or_create_by_external_key(
            symbiote_id=symbiote_id,
            external_key="user1:compose",
            goal="Compose help",
        )
        assert sess.external_key == "user1:compose"
        assert sess.goal == "Compose help"
        assert sess.status == "active"

    def test_returns_existing_active(self, sessions: SessionManager, symbiote_id: str) -> None:
        sess1 = sessions.get_or_create_by_external_key(
            symbiote_id=symbiote_id,
            external_key="user1:page",
        )
        sess2 = sessions.get_or_create_by_external_key(
            symbiote_id=symbiote_id,
            external_key="user1:page",
        )
        assert sess1.id == sess2.id

    def test_reopens_closed_session(self, sessions: SessionManager, symbiote_id: str) -> None:
        sess = sessions.get_or_create_by_external_key(
            symbiote_id=symbiote_id,
            external_key="user1:article",
        )
        sessions.close(sess.id)

        reopened = sessions.get_or_create_by_external_key(
            symbiote_id=symbiote_id,
            external_key="user1:article",
        )
        assert reopened.id == sess.id
        assert reopened.status == "active"

    def test_different_keys_create_different_sessions(
        self, sessions: SessionManager, symbiote_id: str
    ) -> None:
        s1 = sessions.get_or_create_by_external_key(
            symbiote_id=symbiote_id, external_key="user1:page1"
        )
        s2 = sessions.get_or_create_by_external_key(
            symbiote_id=symbiote_id, external_key="user1:page2"
        )
        assert s1.id != s2.id

    def test_different_symbiotes_same_key(self, sessions: SessionManager, adapter: SQLiteAdapter) -> None:
        mgr = IdentityManager(storage=adapter)
        sym1 = mgr.create(name="Bot1", role="a")
        sym2 = mgr.create(name="Bot2", role="b")

        s1 = sessions.get_or_create_by_external_key(
            symbiote_id=sym1.id, external_key="shared_key"
        )
        s2 = sessions.get_or_create_by_external_key(
            symbiote_id=sym2.id, external_key="shared_key"
        )
        assert s1.id != s2.id


class TestFindByExternalKey:
    def test_find_existing(self, sessions: SessionManager, symbiote_id: str) -> None:
        sessions.start(symbiote_id=symbiote_id, external_key="find_me")
        found = sessions.find_by_external_key("find_me")
        assert found is not None
        assert found.external_key == "find_me"

    def test_find_nonexistent(self, sessions: SessionManager) -> None:
        assert sessions.find_by_external_key("no_such_key") is None
