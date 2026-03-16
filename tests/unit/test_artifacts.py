"""Tests for ArtifactManager — T-07."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.exceptions import ValidationError
from symbiote.core.identity import IdentityManager
from symbiote.core.models import Artifact
from symbiote.workspace.artifacts import ArtifactManager
from symbiote.workspace.manager import WorkspaceManager


@pytest.fixture()
def adapter(tmp_path: Path) -> SQLiteAdapter:
    db = tmp_path / "artifact_test.db"
    adp = SQLiteAdapter(db_path=db)
    adp.init_schema()
    yield adp
    adp.close()


@pytest.fixture()
def symbiote_id(adapter: SQLiteAdapter) -> str:
    mgr = IdentityManager(storage=adapter)
    sym = mgr.create(name="TestBot", role="assistant")
    return sym.id


@pytest.fixture()
def session_id(adapter: SQLiteAdapter, symbiote_id: str) -> str:
    sid = str(uuid4())
    adapter.execute(
        "INSERT INTO sessions (id, symbiote_id, status, started_at) "
        "VALUES (?, ?, 'active', datetime('now'))",
        (sid, symbiote_id),
    )
    return sid


@pytest.fixture()
def workspace(
    adapter: SQLiteAdapter, symbiote_id: str, tmp_path: Path
) -> tuple[str, Path]:
    """Create a workspace and return (workspace_id, workspace_root_path)."""
    ws_dir = tmp_path / "workspace_root"
    ws_dir.mkdir()
    mgr = WorkspaceManager(storage=adapter)
    ws = mgr.create(symbiote_id=symbiote_id, name="TestWS", root_path=str(ws_dir))
    return ws.id, ws_dir


@pytest.fixture()
def manager(adapter: SQLiteAdapter) -> ArtifactManager:
    return ArtifactManager(storage=adapter)


# ── Register ───────────────────────────────────────────────────────────────


class TestRegister:
    def test_register_with_real_file(
        self,
        manager: ArtifactManager,
        session_id: str,
        workspace: tuple[str, Path],
    ) -> None:
        ws_id, ws_root = workspace
        (ws_root / "output.txt").write_text("hello")

        art = manager.register(
            session_id=session_id,
            workspace_id=ws_id,
            path="output.txt",
            type="file",
            description="test output",
        )
        assert isinstance(art, Artifact)
        assert art.session_id == session_id
        assert art.workspace_id == ws_id
        assert art.path == "output.txt"
        assert art.type == "file"
        assert art.description == "test output"

        fetched = manager.get(art.id)
        assert fetched is not None
        assert fetched.id == art.id
        assert fetched.path == "output.txt"

    def test_register_directory(
        self,
        manager: ArtifactManager,
        session_id: str,
        workspace: tuple[str, Path],
    ) -> None:
        ws_id, ws_root = workspace
        (ws_root / "subdir").mkdir()

        art = manager.register(
            session_id=session_id,
            workspace_id=ws_id,
            path="subdir",
            type="directory",
        )
        assert art.type == "directory"
        assert art.description is None

    def test_register_nonexistent_file_raises(
        self,
        manager: ArtifactManager,
        session_id: str,
        workspace: tuple[str, Path],
    ) -> None:
        ws_id, _ = workspace
        with pytest.raises(ValidationError, match="does not exist"):
            manager.register(
                session_id=session_id,
                workspace_id=ws_id,
                path="ghost.txt",
                type="file",
            )


# ── Get ────────────────────────────────────────────────────────────────────


class TestGet:
    def test_get_nonexistent_returns_none(self, manager: ArtifactManager) -> None:
        assert manager.get("does-not-exist") is None


# ── List by Session ────────────────────────────────────────────────────────


class TestListBySession:
    def test_list_returns_correct_artifacts(
        self,
        manager: ArtifactManager,
        session_id: str,
        workspace: tuple[str, Path],
    ) -> None:
        ws_id, ws_root = workspace
        (ws_root / "a.txt").write_text("a")
        (ws_root / "b.txt").write_text("b")

        manager.register(
            session_id=session_id, workspace_id=ws_id, path="a.txt", type="file"
        )
        manager.register(
            session_id=session_id, workspace_id=ws_id, path="b.txt", type="file"
        )

        result = manager.list_by_session(session_id)
        assert len(result) == 2
        paths = {a.path for a in result}
        assert paths == {"a.txt", "b.txt"}

    def test_list_empty(self, manager: ArtifactManager) -> None:
        assert manager.list_by_session("no-such-session") == []


# ── List by Workspace ──────────────────────────────────────────────────────


class TestListByWorkspace:
    def test_list_returns_correct_artifacts(
        self,
        manager: ArtifactManager,
        session_id: str,
        workspace: tuple[str, Path],
    ) -> None:
        ws_id, ws_root = workspace
        (ws_root / "report.md").write_text("# Report")

        manager.register(
            session_id=session_id,
            workspace_id=ws_id,
            path="report.md",
            type="report",
            description="weekly report",
        )

        result = manager.list_by_workspace(ws_id)
        assert len(result) == 1
        assert result[0].path == "report.md"
        assert result[0].type == "report"


# ── Verify Exists ──────────────────────────────────────────────────────────


class TestVerifyExists:
    def test_verify_true_when_present(
        self,
        manager: ArtifactManager,
        session_id: str,
        workspace: tuple[str, Path],
    ) -> None:
        ws_id, ws_root = workspace
        (ws_root / "present.txt").write_text("here")

        art = manager.register(
            session_id=session_id, workspace_id=ws_id, path="present.txt", type="file"
        )
        assert manager.verify_exists(art.id) is True

    def test_verify_false_after_deletion(
        self,
        manager: ArtifactManager,
        session_id: str,
        workspace: tuple[str, Path],
    ) -> None:
        ws_id, ws_root = workspace
        target = ws_root / "ephemeral.txt"
        target.write_text("temp")

        art = manager.register(
            session_id=session_id, workspace_id=ws_id, path="ephemeral.txt", type="file"
        )
        target.unlink()
        assert manager.verify_exists(art.id) is False
