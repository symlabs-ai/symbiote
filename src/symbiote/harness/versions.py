"""HarnessVersionRepository — version and retrieve evolvable harness texts.

Each symbiote can have its own version of evolvable text components
(tool_instructions, injection messages, compaction format, etc.).
When no custom version exists, the caller falls back to the hardcoded default.

Versions are numbered sequentially per (symbiote_id, component) and
track avg_score to enable rollback when a new version underperforms.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from symbiote.core.ports import StoragePort


class HarnessVersionRepository:
    """CRUD for harness text versions with score tracking and rollback."""

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage

    def get_active(self, symbiote_id: str, component: str) -> str | None:
        """Return the active version's content, or None if no custom version exists."""
        row = self._storage.fetch_one(
            "SELECT content FROM harness_versions "
            "WHERE symbiote_id = ? AND component = ? AND is_active = 1 "
            "ORDER BY version DESC LIMIT 1",
            (symbiote_id, component),
        )
        return row["content"] if row else None

    def get_active_version(self, symbiote_id: str, component: str) -> dict | None:
        """Return the full active version row as a dict, or None."""
        row = self._storage.fetch_one(
            "SELECT * FROM harness_versions "
            "WHERE symbiote_id = ? AND component = ? AND is_active = 1 "
            "ORDER BY version DESC LIMIT 1",
            (symbiote_id, component),
        )
        return dict(row) if row else None

    def create_version(
        self,
        symbiote_id: str,
        component: str,
        content: str,
        parent_version: int | None = None,
    ) -> int:
        """Create a new version. Returns the version number.

        The new version is immediately active. Previous versions for the
        same (symbiote_id, component) are deactivated.
        """
        # Determine next version number
        row = self._storage.fetch_one(
            "SELECT MAX(version) as max_v FROM harness_versions "
            "WHERE symbiote_id = ? AND component = ?",
            (symbiote_id, component),
        )
        next_version = (row["max_v"] or 0) + 1 if row else 1

        # Deactivate previous versions
        self._storage.execute(
            "UPDATE harness_versions SET is_active = 0 "
            "WHERE symbiote_id = ? AND component = ? AND is_active = 1",
            (symbiote_id, component),
        )

        # Insert new version
        self._storage.execute(
            "INSERT INTO harness_versions "
            "(id, symbiote_id, component, version, content, "
            "avg_score, session_count, is_active, created_at, parent_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid4()),
                symbiote_id,
                component,
                next_version,
                content,
                0.0,
                0,
                1,
                datetime.now(tz=UTC).isoformat(),
                parent_version,
            ),
        )
        return next_version

    def update_score(
        self, symbiote_id: str, component: str, score: float
    ) -> None:
        """Incrementally update the avg_score of the active version.

        Uses running average: new_avg = (old_avg * count + score) / (count + 1).
        """
        row = self.get_active_version(symbiote_id, component)
        if row is None:
            return

        old_avg = row["avg_score"] or 0.0
        count = row["session_count"] or 0
        new_avg = (old_avg * count + score) / (count + 1)

        self._storage.execute(
            "UPDATE harness_versions SET avg_score = ?, session_count = ? "
            "WHERE id = ?",
            (round(new_avg, 4), count + 1, row["id"]),
        )

    def rollback(self, symbiote_id: str, component: str) -> bool:
        """Deactivate the current version and reactivate the previous one.

        Returns True if rollback succeeded, False if no previous version exists.
        """
        current = self.get_active_version(symbiote_id, component)
        if current is None:
            return False

        current_version = current["version"]

        # Deactivate current
        self._storage.execute(
            "UPDATE harness_versions SET is_active = 0 WHERE id = ?",
            (current["id"],),
        )

        # Reactivate previous version
        prev = self._storage.fetch_one(
            "SELECT id FROM harness_versions "
            "WHERE symbiote_id = ? AND component = ? AND version < ? "
            "ORDER BY version DESC LIMIT 1",
            (symbiote_id, component, current_version),
        )
        if prev is None:
            return False

        self._storage.execute(
            "UPDATE harness_versions SET is_active = 1 WHERE id = ?",
            (prev["id"],),
        )
        return True

    def list_versions(
        self, symbiote_id: str, component: str, limit: int = 10
    ) -> list[dict]:
        """Return version history for a component, most recent first."""
        return self._storage.fetch_all(
            "SELECT * FROM harness_versions "
            "WHERE symbiote_id = ? AND component = ? "
            "ORDER BY version DESC LIMIT ?",
            (symbiote_id, component, limit),
        )
