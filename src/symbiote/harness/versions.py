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

_UNSET = object()


class HarnessVersionRepository:
    """CRUD for harness text versions with score tracking and rollback.

    Version 0 of a component is the *baseline*: a never-active row that
    accumulates the scores of sessions run with the built-in default text,
    so the first evolved version (whose parent is the default) can be
    compared and rolled back like any other.
    """

    BASELINE_VERSION = 0

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage

    def get_active(
        self, symbiote_id: str, component: str, tool_mode: str | None = None,
    ) -> str | None:
        """Return the active version's content, or None if no custom version exists.

        When *tool_mode* is provided, looks up a mode-specific variant first
        (e.g. ``tool_instructions:instant``).  Falls back to the generic
        component key if no mode-specific version exists.  This enables
        independent evolution of harness texts per execution mode.
        """
        if tool_mode is not None:
            mode_key = f"{component}:{tool_mode}"
            row = self._storage.fetch_one(
                "SELECT content FROM harness_versions "
                "WHERE symbiote_id = ? AND component = ? AND is_active = 1 "
                "ORDER BY version DESC LIMIT 1",
                (symbiote_id, mode_key),
            )
            if row:
                return row["content"]

        # Fallback: generic component
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
        parent_version: int | None | object = _UNSET,
    ) -> int:
        """Create a new version. Returns the version number.

        The new version is immediately active. Previous versions for the
        same (symbiote_id, component) are deactivated. ``parent_version``
        defaults to the version active right now (or the default text when
        none is); ``None`` means "derived from the default text".
        """
        if parent_version is _UNSET:
            active = self.get_active_version(symbiote_id, component)
            parent_version = active["version"] if active else None
        if parent_version is None:
            parent_version = self.BASELINE_VERSION
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
            # Session ran with the built-in default text: score the baseline.
            row = self._baseline(symbiote_id, component, create=True)

        old_avg = row["avg_score"] or 0.0
        count = row["session_count"] or 0
        new_avg = (old_avg * count + score) / (count + 1)

        self._storage.execute(
            "UPDATE harness_versions SET avg_score = ?, session_count = ? "
            "WHERE id = ?",
            (round(new_avg, 4), count + 1, row["id"]),
        )

    def rollback(self, symbiote_id: str, component: str) -> bool:
        """Deactivate the current version and reactivate its parent.

        The parent is the version this one was derived from — not "the
        previous number": after rolling v2 back to v1 and evolving v3 from
        v1, rolling v3 back returns to v1, never to the rejected v2. When the
        parent is the default text, no version stays active and the default
        applies again.

        Returns True if a version was rolled back, False if none is active.
        """
        current = self.get_active_version(symbiote_id, component)
        if current is None:
            return False

        parent = current.get("parent_version") or self.BASELINE_VERSION
        parent_row = None
        if parent != self.BASELINE_VERSION:
            parent_row = self._storage.fetch_one(
                "SELECT id FROM harness_versions "
                "WHERE symbiote_id = ? AND component = ? AND version = ?",
                (symbiote_id, component, parent),
            )

        self._storage.execute(
            "UPDATE harness_versions SET is_active = 0 WHERE id = ?",
            (current["id"],),
        )
        if parent_row is not None:
            self._storage.execute(
                "UPDATE harness_versions SET is_active = 1 WHERE id = ?",
                (parent_row["id"],),
            )
        return True

    def get_baseline(self, symbiote_id: str, component: str) -> dict | None:
        """Score row of the built-in default text (version 0), if any."""
        return self._baseline(symbiote_id, component, create=False)

    def _baseline(self, symbiote_id: str, component: str, *, create: bool) -> dict | None:
        row = self._storage.fetch_one(
            "SELECT * FROM harness_versions "
            "WHERE symbiote_id = ? AND component = ? AND version = ?",
            (symbiote_id, component, self.BASELINE_VERSION),
        )
        if row is None and create:
            self._storage.execute(
                "INSERT INTO harness_versions "
                "(id, symbiote_id, component, version, content, "
                "avg_score, session_count, is_active, created_at, parent_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid4()), symbiote_id, component, self.BASELINE_VERSION, "",
                    0.0, 0, 0, datetime.now(tz=UTC).isoformat(), None,
                ),
            )
            return self._baseline(symbiote_id, component, create=False)
        return dict(row) if row else None

    def list_versions(
        self, symbiote_id: str, component: str, limit: int = 10
    ) -> list[dict]:
        """Return version history for a component, most recent first."""
        return self._storage.fetch_all(
            "SELECT * FROM harness_versions "
            "WHERE symbiote_id = ? AND component = ? AND version > 0 "
            "ORDER BY version DESC LIMIT ?",
            (symbiote_id, component, limit),
        )
