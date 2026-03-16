"""Tests for WorkspaceManager — T-06."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.exceptions import EntityNotFoundError, ValidationError
from symbiote.core.identity import IdentityManager
from symbiote.core.models import Workspace
from symbiote.workspace.manager import WorkspaceManager


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "workspace_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    """Create a symbiote row and return its ID."""
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name="TestBot", role="assistant")
    return sym.id


@pytest.fixture()
def session_id(adapter: SQLiteAdapter, symbiote_id: str) -> str:
    """Create a session row and return its ID."""
    sid = str(uuid4())
    adapter.execute(
        "INSERT INTO sessions (id, symbiote_id, status, started_at) "
        "VALUES (?, ?, 'active', datetime('now'))",
        (sid, symbiote_id),
    )
    return sid


@pytest.fixture()
def manager(adapter: SQLiteAdapter) -> WorkspaceManager:
    return WorkspaceManager(storage=adapter)


# ── Create ──────────────────────────────────────────────────────────────────


class TestCreate:
    def test_create_persisted_and_retrievable(
        self, manager: WorkspaceManager, symbiote_id: str, tmp_path: Path
    ) -> None:
        ws_dir = tmp_path / "project"
        ws_dir.mkdir()
        ws = manager.create(
            symbiote_id=symbiote_id,
            name="My Project",
            root_path=str(ws_dir),
        )
        assert isinstance(ws, Workspace)
        assert ws.name == "My Project"
        assert ws.root_path == str(ws_dir)
        assert ws.type == "general"
        assert ws.symbiote_id == symbiote_id

        fetched = manager.get(ws.id)
        assert fetched is not None
        assert fetched.id == ws.id
        assert fetched.name == "My Project"

    def test_create_with_type(
        self, manager: WorkspaceManager, symbiote_id: str, tmp_path: Path
    ) -> None:
        ws_dir = tmp_path / "code_project"
        ws_dir.mkdir()
        ws = manager.create(
            symbiote_id=symbiote_id,
            name="Code",
            root_path=str(ws_dir),
            type="code",
        )
        assert ws.type == "code"

    def test_create_nonexistent_path_raises(
        self, manager: WorkspaceManager, symbiote_id: str, tmp_path: Path
    ) -> None:
        bad_path = str(tmp_path / "does_not_exist")
        with pytest.raises(ValidationError, match="does not exist"):
            manager.create(
                symbiote_id=symbiote_id,
                name="Bad",
                root_path=bad_path,
            )


# ── Get ─────────────────────────────────────────────────────────────────────


class TestGet:
    def test_get_nonexistent_returns_none(self, manager: WorkspaceManager) -> None:
        assert manager.get("does-not-exist") is None


# ── List by Symbiote ────────────────────────────────────────────────────────


class TestListBySymbiote:
    def test_list_returns_correct_workspaces(
        self, manager: WorkspaceManager, symbiote_id: str, tmp_path: Path
    ) -> None:
        d1 = tmp_path / "w1"
        d1.mkdir()
        d2 = tmp_path / "w2"
        d2.mkdir()
        manager.create(symbiote_id=symbiote_id, name="W1", root_path=str(d1))
        manager.create(symbiote_id=symbiote_id, name="W2", root_path=str(d2))

        result = manager.list_by_symbiote(symbiote_id)
        assert len(result) == 2
        names = {w.name for w in result}
        assert names == {"W1", "W2"}

    def test_list_empty(self, manager: WorkspaceManager) -> None:
        assert manager.list_by_symbiote("no-such-symbiote") == []


# ── Set Workdir ─────────────────────────────────────────────────────────────


class TestSetWorkdir:
    def test_set_workdir_updates_session(
        self,
        manager: WorkspaceManager,
        adapter: SQLiteAdapter,
        symbiote_id: str,
        session_id: str,
        tmp_path: Path,
    ) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        ws = manager.create(
            symbiote_id=symbiote_id, name="WS", root_path=str(ws_dir)
        )
        manager.set_workdir(session_id=session_id, workspace_id=ws.id)

        row = adapter.fetch_one(
            "SELECT workspace_id FROM sessions WHERE id = ?", (session_id,)
        )
        assert row is not None
        assert row["workspace_id"] == ws.id

    def test_set_workdir_nonexistent_workspace_raises(
        self, manager: WorkspaceManager, session_id: str
    ) -> None:
        with pytest.raises(EntityNotFoundError, match="not found"):
            manager.set_workdir(session_id=session_id, workspace_id="ghost")

    def test_set_workdir_nonexistent_session_raises(
        self, manager: WorkspaceManager, tmp_path: Path, symbiote_id: str
    ) -> None:
        ws = manager.create(symbiote_id=symbiote_id, name="ws", root_path=str(tmp_path))
        with pytest.raises(EntityNotFoundError, match="not found"):
            manager.set_workdir(session_id="ghost-session", workspace_id=ws.id)


# ── Get Active Workdir ──────────────────────────────────────────────────────


class TestGetActiveWorkdir:
    def test_returns_root_path(
        self,
        manager: WorkspaceManager,
        symbiote_id: str,
        session_id: str,
        tmp_path: Path,
    ) -> None:
        ws_dir = tmp_path / "active_ws"
        ws_dir.mkdir()
        ws = manager.create(
            symbiote_id=symbiote_id, name="Active", root_path=str(ws_dir)
        )
        manager.set_workdir(session_id=session_id, workspace_id=ws.id)

        result = manager.get_active_workdir(session_id)
        assert result == str(ws_dir)

    def test_returns_none_when_no_workspace(
        self, manager: WorkspaceManager, session_id: str
    ) -> None:
        assert manager.get_active_workdir(session_id) is None
