"""ArtifactManager — register, query, and verify workspace artifacts."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from symbiote.adapters.storage.base import StoragePort
from symbiote.core.exceptions import EntityNotFoundError, ValidationError
from symbiote.core.models import Artifact


class ArtifactManager:
    """Manages artifact CRUD and disk-existence verification."""

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage

    # ── public API ─────────────────────────────────────────────────────

    def register(
        self,
        session_id: str,
        workspace_id: str,
        path: str,
        type: str,
        description: str | None = None,
    ) -> Artifact:
        """Create and persist an artifact.

        Raises ``ValueError`` if the file/directory does not exist on disk
        at ``{workspace_root}/{path}``.
        """
        root = self._workspace_root(workspace_id)
        full_path = Path(root) / path
        if not full_path.exists():
            raise ValidationError(
                f"Artifact path does not exist: {full_path}"
            )

        art = Artifact(
            session_id=session_id,
            workspace_id=workspace_id,
            path=path,
            type=type,  # type: ignore[arg-type]
            description=description,
        )
        self._storage.execute(
            "INSERT INTO artifacts "
            "(id, session_id, workspace_id, path, type, description, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                art.id,
                art.session_id,
                art.workspace_id,
                art.path,
                art.type,
                art.description,
                art.created_at.isoformat(),
            ),
        )
        return art

    def get(self, artifact_id: str) -> Artifact | None:
        """Fetch an artifact by ID, returning ``None`` if not found."""
        row = self._storage.fetch_one(
            "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
        )
        if row is None:
            return None
        return self._row_to_artifact(row)

    def list_by_session(self, session_id: str) -> list[Artifact]:
        """Return all artifacts belonging to *session_id*."""
        rows = self._storage.fetch_all(
            "SELECT * FROM artifacts WHERE session_id = ?", (session_id,)
        )
        return [self._row_to_artifact(r) for r in rows]

    def list_by_workspace(self, workspace_id: str) -> list[Artifact]:
        """Return all artifacts belonging to *workspace_id*."""
        rows = self._storage.fetch_all(
            "SELECT * FROM artifacts WHERE workspace_id = ?", (workspace_id,)
        )
        return [self._row_to_artifact(r) for r in rows]

    def verify_exists(self, artifact_id: str) -> bool:
        """Check whether the artifact's file/directory still exists on disk."""
        art = self.get(artifact_id)
        if art is None:
            return False
        root = self._workspace_root(art.workspace_id)
        return (Path(root) / art.path).exists()

    # ── private helpers ────────────────────────────────────────────────

    def _workspace_root(self, workspace_id: str) -> str:
        """Look up the root_path for a workspace from the DB."""
        row = self._storage.fetch_one(
            "SELECT root_path FROM workspaces WHERE id = ?", (workspace_id,)
        )
        if row is None:
            raise EntityNotFoundError("Workspace", workspace_id)
        return row["root_path"]

    @staticmethod
    def _row_to_artifact(row: dict) -> Artifact:
        created = row["created_at"]
        if isinstance(created, str):
            created = datetime.fromisoformat(created)
        return Artifact(
            id=row["id"],
            session_id=row["session_id"],
            workspace_id=row["workspace_id"],
            path=row["path"],
            type=row["type"],
            description=row.get("description"),
            created_at=created,
        )
