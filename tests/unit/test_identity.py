"""Tests for IdentityManager — T-04."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.exceptions import EntityNotFoundError
from symbiote.core.identity import IdentityManager
from symbiote.core.models import Symbiote


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    """Return an initialised SQLiteAdapter backed by a temp file."""
    db = tmp_path / "identity_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def manager(adapter: SQLiteAdapter) -> IdentityManager:
    return IdentityManager(storage=adapter)


# ── Create ──────────────────────────────────────────────────────────────────


class TestCreate:
    def test_create_with_defaults(self, manager: IdentityManager) -> None:
        sym = manager.create(name="TestBot", role="assistant")
        assert isinstance(sym, Symbiote)
        assert sym.name == "TestBot"
        assert sym.role == "assistant"
        assert sym.persona_json == {}
        assert sym.owner_id is None
        assert sym.status == "active"
        assert sym.id  # non-empty

    def test_create_persisted_and_retrievable(
        self, manager: IdentityManager
    ) -> None:
        sym = manager.create(name="Bot", role="coder")
        fetched = manager.get(sym.id)
        assert fetched is not None
        assert fetched.id == sym.id
        assert fetched.name == "Bot"
        assert fetched.role == "coder"

    def test_create_with_full_persona(self, manager: IdentityManager) -> None:
        persona = {
            "tone": "friendly",
            "expertise": ["python", "testing"],
            "quirks": {"emoji_usage": False},
        }
        sym = manager.create(
            name="PersonaBot",
            role="advisor",
            persona=persona,
            owner_id="user-42",
        )
        assert sym.persona_json == persona
        assert sym.owner_id == "user-42"

        fetched = manager.get(sym.id)
        assert fetched is not None
        assert fetched.persona_json == persona
        assert fetched.owner_id == "user-42"


# ── Get ─────────────────────────────────────────────────────────────────────


class TestGet:
    def test_get_nonexistent_returns_none(
        self, manager: IdentityManager
    ) -> None:
        assert manager.get("does-not-exist") is None


# ── Update Persona ──────────────────────────────────────────────────────────


class TestUpdatePersona:
    def test_update_persona_persists_new_value(
        self, manager: IdentityManager
    ) -> None:
        sym = manager.create(name="Bot", role="coder", persona={"v": 1})
        updated = manager.update_persona(sym.id, {"v": 2, "extra": True})
        assert updated.persona_json == {"v": 2, "extra": True}

        fetched = manager.get(sym.id)
        assert fetched is not None
        assert fetched.persona_json == {"v": 2, "extra": True}

    def test_update_persona_changes_updated_at(
        self, manager: IdentityManager
    ) -> None:
        sym = manager.create(name="Bot", role="coder")
        original_updated_at = sym.updated_at
        # small sleep to guarantee timestamp difference
        time.sleep(0.05)
        updated = manager.update_persona(sym.id, {"new": True})
        assert updated.updated_at > original_updated_at

    def test_update_persona_creates_audit_trail(
        self, manager: IdentityManager, adapter: SQLiteAdapter
    ) -> None:
        old_persona = {"version": 1}
        new_persona = {"version": 2}
        sym = manager.create(name="Bot", role="coder", persona=old_persona)
        manager.update_persona(sym.id, new_persona)

        rows = adapter.fetch_all(
            "SELECT * FROM persona_audit WHERE symbiote_id = ?", (sym.id,)
        )
        assert len(rows) == 1
        assert json.loads(rows[0]["old_persona_json"]) == old_persona
        assert json.loads(rows[0]["new_persona_json"]) == new_persona
        assert rows[0]["symbiote_id"] == sym.id
        assert rows[0]["changed_at"] is not None

    def test_update_persona_nonexistent_raises(
        self, manager: IdentityManager
    ) -> None:
        with pytest.raises(EntityNotFoundError, match="not found"):
            manager.update_persona("ghost-id", {"x": 1})


# ── List All ────────────────────────────────────────────────────────────────


class TestListAll:
    def test_list_all_empty(self, manager: IdentityManager) -> None:
        assert manager.list_all() == []

    def test_list_all_returns_all(self, manager: IdentityManager) -> None:
        manager.create(name="A", role="r1")
        manager.create(name="B", role="r2")
        manager.create(name="C", role="r3")
        result = manager.list_all()
        assert len(result) == 3
        names = {s.name for s in result}
        assert names == {"A", "B", "C"}


# ── Persistence across restart ──────────────────────────────────────────────


class TestPersistenceRestart:
    def test_identity_survives_restart(self, tmp_path: Path) -> None:
        db_path = tmp_path / "restart_test.db"

        # First "session"
        adp1 = SQLiteAdapter(db_path=db_path)
        adp1.init_schema()
        mgr1 = IdentityManager(storage=adp1)
        sym = mgr1.create(
            name="Persistent", role="keeper", persona={"memory": "long"}
        )
        sym_id = sym.id
        adp1.close()

        # Second "session" — new adapter, same DB
        adp2 = SQLiteAdapter(db_path=db_path)
        adp2.init_schema()
        mgr2 = IdentityManager(storage=adp2)
        fetched = mgr2.get(sym_id)

        assert fetched is not None
        assert fetched.name == "Persistent"
        assert fetched.role == "keeper"
        assert fetched.persona_json == {"memory": "long"}
        adp2.close()
