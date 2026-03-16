"""WorkspaceManager — create, query, and link workspaces to sessions."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from symbiote.adapters.storage.base import StoragePort
from symbiote.core.exceptions import EntityNotFoundError, ValidationError
from symbiote.core.models import Workspace


class WorkspaceManager:
    """Manages workspace CRUD and session-workspace linking."""

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage

    # ── public API ─────────────────────────────────────────────────────

    def create(
        self,
        symbiote_id: str,
        name: str,
        root_path: str,
        type: str = "general",
    ) -> Workspace:
        """Create and persist a workspace.

        Raises ValueError if *root_path* does not exist on disk.
        """
        if not Path(root_path).exists():
            raise ValidationError(f"root_path does not exist: {root_path}")

        ws = Workspace(
            symbiote_id=symbiote_id,
            name=name,
            root_path=root_path,
            type=type,  # type: ignore[arg-type]
        )
        self._storage.execute(
            "INSERT INTO workspaces (id, symbiote_id, name, root_path, type, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                ws.id,
                ws.symbiote_id,
                ws.name,
                ws.root_path,
                ws.type,
                ws.created_at.isoformat(),
            ),
        )
        return ws

    def get(self, workspace_id: str) -> Workspace | None:
        """Fetch a workspace by ID, returning None if not found."""
        row = self._storage.fetch_one(
            "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
        )
        if row is None:
            return None
        return self._row_to_workspace(row)

    def list_by_symbiote(self, symbiote_id: str) -> list[Workspace]:
        """Return all workspaces belonging to *symbiote_id*."""
        rows = self._storage.fetch_all(
            "SELECT * FROM workspaces WHERE symbiote_id = ?", (symbiote_id,)
        )
        return [self._row_to_workspace(r) for r in rows]

    def set_workdir(self, session_id: str, workspace_id: str) -> None:
        """Link a session to a workspace.

        Raises ValueError if the workspace does not exist.
        """
        session_row = self._storage.fetch_one(
            "SELECT id FROM sessions WHERE id = ?", (session_id,)
        )
        if session_row is None:
            raise EntityNotFoundError("Session", session_id)

        if self.get(workspace_id) is None:
            raise EntityNotFoundError("Workspace", workspace_id)

        self._storage.execute(
            "UPDATE sessions SET workspace_id = ? WHERE id = ?",
            (workspace_id, session_id),
        )

    def get_active_workdir(self, session_id: str) -> str | None:
        """Return the root_path of the workspace linked to *session_id*, or None."""
        row = self._storage.fetch_one(
            "SELECT workspace_id FROM sessions WHERE id = ?", (session_id,)
        )
        if row is None or row["workspace_id"] is None:
            return None

        ws = self.get(row["workspace_id"])
        if ws is None:
            return None
        return ws.root_path

    # ── private helpers ────────────────────────────────────────────────

    @staticmethod
    def _row_to_workspace(row: dict) -> Workspace:
        created = row["created_at"]
        if isinstance(created, str):
            created = datetime.fromisoformat(created)
        return Workspace(
            id=row["id"],
            symbiote_id=row["symbiote_id"],
            name=row["name"],
            root_path=row["root_path"],
            type=row.get("type", "general"),
            created_at=created,
        )
